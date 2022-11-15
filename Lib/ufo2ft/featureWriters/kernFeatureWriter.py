from __future__ import annotations

import itertools
import logging
from types import SimpleNamespace

from fontTools import unicodedata
from fontTools.unicodedata import script_horizontal_direction

from ufo2ft.constants import COMMON_SCRIPT, INDIC_SCRIPTS, USE_SCRIPTS
from ufo2ft.featureWriters import BaseFeatureWriter, ast
from ufo2ft.util import DFLT_SCRIPTS, classifyGlyphs, quantize, unicodeScriptDirection

LOGGER = logging.getLogger(__name__)

SIDE1_PREFIX = "public.kern1."
SIDE2_PREFIX = "public.kern2."

# In HarfBuzz the 'dist' feature is automatically enabled for these shapers:
#   src/hb-ot-shape-complex-myanmar.cc
#   src/hb-ot-shape-complex-use.cc
#   src/hb-ot-shape-complex-indic.cc
#   src/hb-ot-shape-complex-khmer.cc
# We derived the list of scripts associated to each dist-enabled shaper from
# `hb_ot_shape_complex_categorize` in src/hb-ot-shape-complex-private.hh
DIST_ENABLED_SCRIPTS = set(INDIC_SCRIPTS) | set(["Khmr", "Mymr"]) | set(USE_SCRIPTS)

RTL_BIDI_TYPES = {"R", "AL"}
LTR_BIDI_TYPES = {"L", "AN", "EN"}
BAD_BIDIS = {"R", "L"}


def unicodeBidiType(uv):
    """Return "R" for characters with RTL direction, or "L" for LTR (whether
    'strong' or 'weak'), or None for neutral direction.
    """
    char = chr(uv)
    bidiType = unicodedata.bidirectional(char)
    if bidiType in RTL_BIDI_TYPES:
        return "R"
    elif bidiType in LTR_BIDI_TYPES:
        return "L"
    else:
        return None


class KerningPair:

    __slots__ = ("side1", "side2", "value")

    def __init__(self, side1, side2, value):
        if isinstance(side1, str):
            self.side1 = ast.GlyphName(side1)
        elif isinstance(side1, ast.GlyphClassDefinition):
            self.side1 = ast.GlyphClassName(side1)
        elif isinstance(side1, (list, set)):
            if len(side1) == 1:
                self.side1 = ast.GlyphName(list(side1)[0])
            else:
                self.side1 = ast.GlyphClass([ast.GlyphName(g) for g in sorted(side1)])
        else:
            raise AssertionError(side1)

        if isinstance(side2, str):
            self.side2 = ast.GlyphName(side2)
        elif isinstance(side2, ast.GlyphClassDefinition):
            self.side2 = ast.GlyphClassName(side2)
        elif isinstance(side2, (list, set)):
            if len(side2) == 1:
                self.side2 = ast.GlyphName(list(side2)[0])
            else:
                self.side2 = ast.GlyphClass([ast.GlyphName(g) for g in sorted(side2)])
        else:
            raise AssertionError(side2)

        self.value = value

    def __lt__(self, other: KerningPair) -> bool:
        if not isinstance(other, KerningPair):
            return NotImplemented

        selfTuple = (
            self.firstIsClass,
            self.secondIsClass,
            tuple(sorted(self.firstGlyphs)),
            tuple(sorted(self.secondGlyphs)),
        )
        otherTuple = (
            other.firstIsClass,
            other.secondIsClass,
            tuple(sorted(other.firstGlyphs)),
            tuple(sorted(other.secondGlyphs)),
        )

        return selfTuple < otherTuple

    @property
    def firstIsClass(self):
        return isinstance(self.side1, (ast.GlyphClassName, ast.GlyphClass))

    @property
    def secondIsClass(self):
        return isinstance(self.side2, (ast.GlyphClassName, ast.GlyphClass))

    @property
    def firstGlyphs(self):
        if self.firstIsClass:
            if isinstance(self.side1, ast.GlyphClassName):
                classDef1 = self.side1.glyphclass
            else:
                classDef1 = self.side1
            return {g.asFea() for g in classDef1.glyphSet()}
        else:
            return {self.side1.asFea()}

    @property
    def secondGlyphs(self):
        if self.secondIsClass:
            if isinstance(self.side2, ast.GlyphClassName):
                classDef2 = self.side2.glyphclass
            else:
                classDef2 = self.side2
            return {g.asFea() for g in classDef2.glyphSet()}
        else:
            return {self.side2.asFea()}

    @property
    def glyphs(self):
        return self.firstGlyphs | self.secondGlyphs

    def __repr__(self):
        return "<{} {} {} {}>".format(
            self.__class__.__name__,
            self.side1,
            self.side2,
            self.value,
        )


class KernFeatureWriter(BaseFeatureWriter):
    """Generates a kerning feature based on groups and rules contained
    in an UFO's kerning data.

    There are currently two possible writing modes:
    2) "skip" (default) will not write anything if the features are already present;
    1) "append" will add additional lookups to an existing feature, if present,
       or it will add a new one at the end of all features.

    If the `quantization` argument is given in the filter options, the resulting
    anchors are rounded to the nearest multiple of the quantization value.
    """

    tableTag = "GPOS"
    features = frozenset(["kern", "dist"])
    options = dict(ignoreMarks=True, quantization=1)

    def setContext(self, font, feaFile, compiler=None):
        ctx = super().setContext(font, feaFile, compiler=compiler)
        ctx.gdefClasses = self.getGDEFGlyphClasses()
        ctx.glyphSet = self.getOrderedGlyphSet()

        # TODO: Also include substitution information from Designspace rules to
        # correctly the scripts of variable substitution glyphs, maybe add
        # `glyphUnicodeMapping: dict[str, int] | None` to `BaseFeatureCompiler`?
        cmap = self.makeUnicodeToGlyphNameMapping()
        gsub = self.compileGSUB()
        scriptGlyphs = classifyGlyphs(self.knownScriptsPerCodepoint, cmap, gsub)
        bidiGlyphs = classifyGlyphs(unicodeBidiType, cmap, gsub)
        ctx.bidiGlyphs = bidiGlyphs

        glyphScripts = {}
        for script, glyphs in scriptGlyphs.items():
            for g in glyphs:
                glyphScripts.setdefault(g, set()).add(script)
        ctx.glyphScripts = glyphScripts

        ctx.kerning = self.getKerningData()

        return ctx

    def shouldContinue(self):
        if not self.context.kerning.pairs:
            self.log.debug("No kerning data; skipped")
            return False

        return super().shouldContinue()

    def _write(self):
        lookups = self._makeKerningLookups()
        if not lookups:
            self.log.debug("kerning lookups empty; skipped")
            return False

        features = self._makeFeatureBlocks(lookups)
        if not features:
            self.log.debug("kerning features empty; skipped")
            return False

        # extend feature file with the new generated statements
        feaFile = self.context.feaFile

        # first add the glyph class definitions
        side1Classes = self.context.kerning.side1Classes
        side2Classes = self.context.kerning.side2Classes
        newClassDefs = []
        for classes in (side1Classes, side2Classes):
            newClassDefs.extend([c for _, c in sorted(classes.items())])

        lookupGroups = []
        for _, lookupGroup in sorted(lookups.items()):
            lookupGroups.extend(lookupGroup.values())

        self._insert(
            feaFile=feaFile,
            classDefs=newClassDefs,
            lookups=lookupGroups,
            features=[features[tag] for tag in ["kern", "dist"] if tag in features],
        )
        return True

    def getKerningData(self):
        side1Classes, side2Classes = self.getKerningClasses()
        pairs = self.getKerningPairs(side1Classes, side2Classes)
        return SimpleNamespace(
            side1Classes=side1Classes, side2Classes=side2Classes, pairs=pairs
        )

    def getKerningGroups(self):
        font = self.context.font
        allGlyphs = self.context.glyphSet
        side1Groups = {}
        side2Groups = {}
        for name, members in font.groups.items():
            # prune non-existent or skipped glyphs
            members = [g for g in members if g in allGlyphs]
            if not members:
                # skip empty groups
                continue
            # skip groups without UFO3 public.kern{1,2} prefix
            if name.startswith(SIDE1_PREFIX):
                side1Groups[name] = members
            elif name.startswith(SIDE2_PREFIX):
                side2Groups[name] = members
        return side1Groups, side2Groups

    def getKerningClasses(self):
        side1Groups, side2Groups = self.getKerningGroups()
        feaFile = self.context.feaFile
        side1Classes = ast.makeGlyphClassDefinitions(
            side1Groups, feaFile, stripPrefix="public."
        )
        side2Classes = ast.makeGlyphClassDefinitions(
            side2Groups, feaFile, stripPrefix="public."
        )
        return side1Classes, side2Classes

    def getKerningPairs(self, side1Classes, side2Classes):
        glyphSet = self.context.glyphSet
        font = self.context.font
        kerning = font.kerning
        quantization = self.options.quantization

        kerning = font.kerning
        result = []
        for (side1, side2), value in kerning.items():
            firstIsClass, secondIsClass = (side1 in side1Classes, side2 in side2Classes)
            # Filter out pairs that reference missing groups or glyphs.
            if not firstIsClass and side1 not in glyphSet:
                continue
            if not secondIsClass and side2 not in glyphSet:
                continue
            # Ignore zero-valued class kern pairs. They are the most general
            # kerns, so they don't override anything else like glyph kerns would
            # and zero is the default.
            if firstIsClass and secondIsClass and value == 0:
                continue
            if firstIsClass:
                side1 = side1Classes[side1]
            if secondIsClass:
                side2 = side2Classes[side2]
            value = quantize(value, quantization)
            result.append(KerningPair(side1, side2, value))

        return result

    @staticmethod
    def _makePairPosRule(pair, rtl=False):
        enumerated = pair.firstIsClass ^ pair.secondIsClass
        valuerecord = ast.ValueRecord(
            xPlacement=pair.value if rtl else None,
            yPlacement=0 if rtl else None,
            xAdvance=pair.value,
            yAdvance=0 if rtl else None,
        )
        return ast.PairPosStatement(
            glyphs1=pair.side1,
            valuerecord1=valuerecord,
            glyphs2=pair.side2,
            valuerecord2=None,
            enumerated=enumerated,
        )

    def _makeKerningLookup(self, name, ignoreMarks=True):
        lookup = ast.LookupBlock(name)
        if ignoreMarks and self.options.ignoreMarks:
            lookup.statements.append(ast.makeLookupFlag("IgnoreMarks"))
        return lookup

    def knownScriptsPerCodepoint(self, uv):
        return unicodedata.script_extension(chr(uv))

    def _makeKerningLookups(self):
        marks = self.context.gdefClasses.mark
        lookups = {}
        pairs = self.context.kerning.pairs
        glyphScripts = self.context.glyphScripts

        if self.options.ignoreMarks:
            basePairs, markPairs = self._splitBaseAndMarkPairs(
                self.context.kerning.pairs, marks
            )
            if basePairs:
                self._makeSplitScriptKernLookups(lookups, basePairs, glyphScripts)
            if markPairs:
                self._makeSplitScriptKernLookups(
                    lookups, markPairs, glyphScripts, ignoreMarks=False, suffix="_marks"
                )
        else:
            self._makeSplitScriptKernLookups(lookups, pairs, glyphScripts)
        return lookups

    def _splitBaseAndMarkPairs(self, pairs, marks):
        basePairs, markPairs = [], []
        if marks:
            for pair in pairs:
                if any(glyph in marks for glyph in pair.glyphs):
                    markPairs.append(pair)
                else:
                    basePairs.append(pair)
        else:
            basePairs[:] = pairs
        return basePairs, markPairs

    def _makeSplitScriptKernLookups(
        self, lookups, pairs, glyphScripts, ignoreMarks=True, suffix=""
    ):
        bidiGlyphs = self.context.bidiGlyphs
        kerningPerScript = splitKerning(pairs, glyphScripts)
        for script, pairs in kerningPerScript.items():
            scriptLookups = lookups.setdefault(script, {})

            key = f"kern_{script}{suffix}"
            lookup = scriptLookups.get(key)
            if not lookup:
                # For neatness:
                lookup = self._makeKerningLookup(
                    key.replace(COMMON_SCRIPT, "Common"),  # For neatness
                    ignoreMarks=ignoreMarks,
                )
                scriptLookups[key] = lookup

            for pair in pairs:
                bidiTypes = {
                    direction
                    for direction, glyphs in bidiGlyphs.items()
                    if not set(pair.glyphs).isdisjoint(glyphs)
                }
                if bidiTypes.issuperset(BAD_BIDIS):
                    LOGGER.info(
                        "Skipping kerning pair <%s %s %s> with ambiguous direction",
                        pair.side1,
                        pair.side2,
                        pair.value,
                    )
                    continue
                scriptIsRtl = script_horizontal_direction(script, "LTR") == "RTL"
                # Numbers are always shaped LTR even in RTL scripts:
                pairIsRtl = "L" not in bidiTypes
                rule = self._makePairPosRule(pair, rtl=scriptIsRtl and pairIsRtl)
                lookup.statements.append(rule)

        # Clean out empty lookups.
        for script, scriptLookups in list(lookups.items()):
            for lookup_name, lookup in list(scriptLookups.items()):
                if not any(
                    stmt
                    for stmt in lookup.statements
                    if not isinstance(stmt, ast.LookupFlagStatement)
                ):
                    del scriptLookups[lookup_name]
            if not scriptLookups:
                del lookups[script]

    def _makeFeatureBlocks(self, lookups):
        features = {}
        if "kern" in self.context.todo:
            kern = ast.FeatureBlock("kern")
            self._registerLookups(kern, lookups)
            if kern.statements:
                features["kern"] = kern
        if "dist" in self.context.todo:
            dist = ast.FeatureBlock("dist")
            self._registerLookups(dist, lookups)
            if dist.statements:
                features["dist"] = dist
        return features

    @staticmethod
    def _registerLookups(
        feature: ast.FeatureBlock, lookups: dict[str, dict[str, ast.LookupBlock]]
    ) -> None:
        # Ensure we have kerning for pure common script runs (e.g. ">1")
        isKernBlock = feature.name == "kern"
        if isKernBlock and COMMON_SCRIPT in lookups:
            ast.addLookupReferences(
                feature, lookups[COMMON_SCRIPT].values(), "DFLT", ["dflt"]
            )

        # Feature blocks use script tags to distinguish what to run for a
        # Unicode script.
        #
        # "Script tags generally correspond to a Unicode script. However, the
        # associations between them may not always be one-to-one, and the
        # OpenType script tags are not guaranteed to be the same as Unicode
        # Script property-value aliases or ISO 15924 script IDs."
        #
        # E.g. {"latn": "Latn", "telu": "Telu", "tel2": "Telu"}
        #
        # Skip DFLT script because we always take care of it above for `kern`.
        # It never occurs in `dist`.
        if isKernBlock:
            scriptsToReference = lookups.keys() - DIST_ENABLED_SCRIPTS
        else:
            scriptsToReference = DIST_ENABLED_SCRIPTS.intersection(lookups.keys())
        for script in sorted(scriptsToReference - DFLT_SCRIPTS):
            for tag in unicodedata.ot_tags_from_script(script):
                # Insert line breaks between statements for niceness :).
                if feature.statements:
                    feature.statements.append(ast.Comment(""))
                # We have something for this script. First add the default
                # lookups, then the script-specific ones
                lookupsForThisScript = []
                for dfltScript in DFLT_SCRIPTS:
                    if dfltScript in lookups:
                        lookupsForThisScript.extend(lookups[dfltScript].values())
                lookupsForThisScript.extend(lookups[script].values())
                # NOTE: We always use the `dflt` language because there is no
                # language-specific kerning to be derived from UFO (kerning.plist)
                # sources and we are independent of what's going on in the rest of
                # the features.fea file.
                ast.addLookupReferences(feature, lookupsForThisScript, tag, ["dflt"])


def splitKerning(pairs, glyphScripts):
    # Split kerning into per-script buckets, so we can post-process them before
    # continuing.
    kerningPerScript = {}
    for pair in pairs:
        for script, splitPair in partitionByScript(pair, glyphScripts):
            kerningPerScript.setdefault(script, []).append(splitPair)

    for pairs in kerningPerScript.values():
        pairs.sort()

    return kerningPerScript


def partitionByScript(pair, glyphScripts):
    """Split a potentially mixed-script pair into pairs that make sense based
    on the dominant script, and yield each combination with its dominant script."""

    # First, partition the pair by their assigned scripts
    allFirstScripts = {}
    allSecondScripts = {}
    for g in pair.firstGlyphs:
        if g not in glyphScripts:
            glyphScripts[g] = set([COMMON_SCRIPT])
        allFirstScripts.setdefault(tuple(glyphScripts[g]), []).append(g)
    for g in pair.secondGlyphs:
        if g not in glyphScripts:
            glyphScripts[g] = set([COMMON_SCRIPT])
        allSecondScripts.setdefault(tuple(glyphScripts[g]), []).append(g)

    # Super common case
    if (
        len(allFirstScripts.keys()) == 1
        and allFirstScripts.keys() == allSecondScripts.keys()
    ):
        for script in list(allFirstScripts.keys())[0]:
            yield script, pair
        return

    # Now let's go through the script combinations
    for firstScripts, secondScripts in itertools.product(
        allFirstScripts.keys(), allSecondScripts.keys()
    ):
        localPair = KerningPair(
            sorted(allFirstScripts[firstScripts]),
            sorted(allSecondScripts[secondScripts]),
            pair.value,
        )
        # Handle very obvious common cases: one script, same on both sides
        if (
            len(firstScripts) == 1
            and len(secondScripts) == 1
            and firstScripts == secondScripts
        ):
            yield firstScripts[0], localPair
        # First is single script, second is common
        elif len(firstScripts) == 1 and set(secondScripts).issubset(DFLT_SCRIPTS):
            yield firstScripts[0], localPair
        # First is common, second is single script
        elif set(firstScripts).issubset(DFLT_SCRIPTS) and len(secondScripts) == 1:
            yield secondScripts[0], localPair
        # One script and it's different on both sides and it's not common
        elif len(firstScripts) == 1 and len(secondScripts) == 1:
            logger = ".".join([pair.__class__.__module__, pair.__class__.__name__])
            logging.getLogger(logger).info(
                "Mixed script kerning pair %s ignored" % localPair
            )
            pass
        else:
            # At this point, we have a pair which has different sets of
            # scripts on each side, and we have to find commonalities.
            # For example, the pair
            #   [A A-cy] {Latn, Cyrl}  --  [T Te-cy Tau] {Latn, Cyrl, Grek}
            # must be split into
            #   A -- T
            #   A-cy -- Te-cy
            # and the Tau ignored.
            commonScripts = set(firstScripts) & set(secondScripts)
            commonFirstGlyphs = set()
            commonSecondGlyphs = set()
            for scripts, g in allFirstScripts.items():
                if commonScripts.issubset(set(scripts)):
                    commonFirstGlyphs |= set(g)
            for scripts, g in allSecondScripts.items():
                if commonScripts.issubset(set(scripts)):
                    commonSecondGlyphs |= set(g)
            for common in commonScripts:
                localPair = KerningPair(
                    commonFirstGlyphs, commonSecondGlyphs, pair.value
                )
                yield common, localPair
