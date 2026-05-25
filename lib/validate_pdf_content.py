"""Validateur PDF anti-bad-match.

Vérifie que le contenu page 1 d'un PDF correspond au domaine musique/linguistique/CS
attendu, et n'est PAS un bad match d'homonymie (entomologie, biologie médicale,
journal local, fantasy novel, etc.).

Cas réels recensés ayant motivé ce module :
- Wilson 2021 → arachnologie
- Ariza 2011 → biologie moléculaire
- Huron 2002 → journal local
- Hamanaka 2020 → politologie
- Goguen 1992 → roman fantasy
- Zeng 2021 → immunopharmacologie
- Poeppel 2011 → électronique de puissance
"""
import re
import subprocess
import zipfile
from pathlib import Path


def extract_epub_text(path, max_chars: int = 4000) -> str:
    """Extrait le texte des premiers XHTML d'un EPUB (zip de XHTML), tags strippés.

    EPUB = format VALIDE (RTFM l'indexe). On extrait le texte pour le contrôle
    d'identité/domaine, exactement comme pour un PDF — un EPUB valide ne garantit
    PAS le bon contenu (homonymie possible).
    """
    import html as _html
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return ""
    names = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
    # ordre de lecture : opf spine idéalement, à défaut ordre d'archive
    out = []
    total = 0
    for n in names:
        try:
            raw = z.read(n).decode("utf-8", errors="ignore")
        except Exception:
            continue
        txt = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            out.append(txt)
            total += len(txt)
        if total >= max_chars:
            break
    return " ".join(out)[:max_chars]

# Keywords clairement hors-domaine musique/linguistique/CS
OFF_DOMAIN_KEYWORDS = {
    # Biologie/médecine
    "biology": ["molecular biology", "gene expression", "rna self-cleavage", "dna sequenc",
                "protein", "enzyme", "antibody", "mrna", "transcription factor"],
    "medicine": ["clinical trial", "patient", "syndrome", "tumor", "cancer", "carcinoma",
                 "oncology", "cardiology", "immunopharmacology", "immunology", "neurology",
                 "diabetes", "hypertension", "disease", "therap"],
    "zoology": ["arachnid", "spider", "butterfly", "butterflies", "mammal", "insect",
                "fish ", "fishes", "bird ", "birds", "taxonomy", "species ", "larva",
                "entomology", "wildlife"],
    "botany": ["flora", "fauna", "plant species", "horticulture", "agronomy"],
    "geography_news": ["township", "weekly news", "newspaper", "river weekly", "prairie",
                       "the daily", "the times", "gazette"],
    "engineering_hardware": ["transistor", "microprocessor", "semiconductor", "voltage",
                              "fpga", "circuit board", "vlsi", "electronics of power"],
    "chemistry": ["polymer synthesis", "catalyst", "reagent", "molecular compound",
                  "oxidation kinetic", "chemical synthesis"],
    "real_estate_finance": ["real estate", "marketing strategy", "retail sales",
                             "financial portfolio", "macroeconomic"],
    "fantasy_literature": ["dragon", "wizard", "tundra fris", "fantasy novel", "epic saga"],
    "politics": ["election", "political party", "voting behaviour", "parliament", "democracy"],
    # Extensions ajoutées 2026-05-18 après détection 28% homonymies sur batch bibliography_seed
    "dentistry_health": ["dental implant", "ophthalmology", "neurosurgery", "spinal",
                          "weight loss", "nutrition", "dietetica", "rainwater", "wetland treatment"],
    "fantasy_horror": ["nightmare reader", "abramelin", "guardian angel", "macabre",
                       "horror anthology", "occult"],
    "self_help_parenting": ["working parent", "workparent", "hado power", "code red",
                             "10 pound takedown", "21st century challenge", "leadership style"],
    "engineering_other": ["wetland treatment", "free trade agreement", "wto compatibility",
                           "structural analysis system", "adina", "engineering plasticity"],
    "humanities_offtopic": ["medieval latin", "mittellatein", "gentle art of columning",
                             "tibetolog", "wiener studien", "husserliana", "transzendentale logik",
                             "from gods to god", "geometria dos tra"],
    "consumer_tech": ["javascript reference", "macintosh", "concise system 7"],
    "general_books_homonymy": ["jazz transatlantic", "data power", "music navigation",
                                "advancing women in leadership", "kapitalizm ve demokrasi",
                                "por la cocina", "watch my tracer", "bellwether media",
                                "blastoff readers"],
    "rock_music_book": ["john mayer", "no such thing", "your body is a wonderland",
                         "room for squares"],
    "russian_math_journal": ["рудн", "rudn university"],
    # Extensions ajoutées 2026-05-18 (post cascade 305 actionables — 6 wrong-paper substitutions)
    "chemistry_organic": ["catalyz", "hydroamin", "diastereomer", "tandem reaction",
                           "aza-diels-alder", "enantiomer", "fused pyridine", "cheminform"],
    "biology_connectomics": ["connectomics", "neuropil", "synaptogenesis", "drosophila",
                              "c. elegans", "neural circuit reconstruction"],
    "demography": ["fertility rate", "demographic transition", "life expectancy", "mortality rate"],
    "cryptocurrency": ["bitcoin", "blockchain", "cryptocurrency", "satoshi nakamoto"],
}

# Mots de titre trop génériques dans nos domaines (musique/ICM/MIR/CS) : ils
# apparaissent dans des centaines de papiers et ne discriminent PAS une homonymie.
# Détecté 2026-05-20 (batch L) : taux de substitution ~56% sur noms indiens courts
# (Belle/Singh/Sharma/Joshi) car les titres ICM partagent "raga/indian/classical/music".
# On les retire du calcul de match-titre verbatim → il faut un mot DISTINCTIF.
GENERIC_TITLE_WORDS = {
    "music", "musical", "indian", "classical", "carnatic", "hindustani", "raga", "ragas",
    "raag", "pitch", "melodic", "melody", "rhythm", "rhythmic", "audio", "analysis",
    "model", "models", "modeling", "modelling", "learning", "system", "systems",
    "approach", "approaches", "method", "methods", "based", "using", "towards", "study",
    "studies", "automatic", "detection", "recognition", "representation", "representations",
    "signal", "processing", "computational", "music21", "musicology", "tradition",
    "traditional", "performance", "analysis", "feature", "features", "estimation",
    "framework", "application", "applications", "introduction", "overview", "survey",
    "review", "toward", "novel", "improved", "efficient", "robust", "scale", "large",
}


def distinctive_title_words(title: str, min_len: int = 5) -> list[str]:
    """Mots du titre ≥min_len lettres qui ne sont PAS génériques (discriminants)."""
    words = [w.lower() for w in re.findall(r"[a-zA-Z]+", title or "") if len(w) >= min_len]
    return [w for w in words if w not in GENERIC_TITLE_WORDS]


# Keywords musique/linguistique/CS (positifs)
ON_DOMAIN_KEYWORDS = [
    # Musique
    "music", "harmonic", "harmony", "melody", "melodic", "rhythm", "rhythmic", "tonal",
    "tonality", "audio", "midi", "musical", "composer", "composition", "chord", "pitch",
    "signal processing", "spectrogram", "synthesizer", "instrument", "voice", "song",
    "tempo", "meter", "beat", "tabla", "raga", "raag", "mode", "scale", "interval",
    # Linguistique
    "grammar", "grammatical", "parser", "parsing", "syntactic", "syntax", "semantic",
    "semantics", "formal language", "automaton", "automata", "linguistic", "natural language",
    "phoneme", "morpholog", "lexical", "tokeniz", "speech", "phonetic", "prosody",
    # CS / théorie
    "compiler", "compiling", "algorithm", "computation", "computability", "complexity",
    "recursion", "context-free", "context free", "regular expression", "lexer",
    "deterministic", "nondeterministic", "polynomial time", "np-hard", "decidab",
    "pcfg", "ccg", "tag (tree adjoining", "mcfg", "lambda calcul",
    # Cognition / linguistique cognitive
    "cognition", "cognitive", "perception", "psycholinguistic", "psychoacoustic",
    # IA / ML pertinent
    "neural network", "transformer", "deep learning", "machine learning",
]


def extract_page1(pdf_path: Path, max_chars: int = 4000) -> str:
    """Extrait le texte page 1 du PDF."""
    try:
        out = subprocess.run(
            ["pdftotext", "-l", "1", str(pdf_path), "-"],
            capture_output=True, timeout=20, text=True, errors="ignore",
        )
        if out.returncode == 0:
            return out.stdout[:max_chars]
    except Exception:
        pass
    return ""


def probe_pdf_health(pdf_path, timeout: int = 15) -> tuple[str, str]:
    """Diagnostic DÉTERMINISTE de l'état physique d'un fichier (lit le fichier réel,
    jamais une valeur DB).

    Motivation (2026-05-20, CR diagnostic RTFM) : l'ancien pipeline confondait
    "scan sans texte" et "fichier corrompu/illisible" — les deux donnaient
    pdftotext vide → tous deux versés à tort dans `awaiting_rtfm_ocr`. Or l'OCR
    ne peut RIEN faire d'un fichier corrompu (même backend pdfium qui échoue).
    On distingue donc 4 cas pour router correctement.

    Retourne (category, detail) avec category ∈ :
      - "missing"            : fichier absent
      - "too_small"          : < 3 Ko (placeholder/erreur DL)
      - "wrong_format"       : magic ≠ %PDF (ex. PK = zip/epub déguisé) → convertir
      - "corrupt_unreadable" : pdfinfo échoue/timeout → re-acquérir (PAS d'OCR)
      - "scan_needs_ocr"     : s'ouvre mais < 20 chars/page → OCR pertinent
      - "ok_has_text"        : texte natif extractible → valider normalement
    Seuil 20 chars/page aligné sur le diagnostic RTFM déterministe.
    """
    p = Path(pdf_path)
    if not p.exists():
        return "missing", "file_absent"
    size = p.stat().st_size
    if size < 3000:
        return "too_small", f"{size}B"
    with open(p, "rb") as f:
        magic = f.read(5)
    if magic[:4] == b"PK\x03\x04":
        # zip : EPUB (format VALIDE, RTFM-géré) si mimetype application/epub+zip,
        # sinon vrai mauvais format (docx/zip quelconque).
        try:
            z = zipfile.ZipFile(p)
            mt = z.read("mimetype").decode(errors="ignore").strip() if "mimetype" in z.namelist() else ""
        except Exception:
            mt = ""
        if mt == "application/epub+zip":
            return "ok_epub", "epub (format valide — contrôle contenu requis)"
        return "wrong_format", f"zip_non_epub (mimetype={mt or 'absent'})"
    if not magic.startswith(b"%PDF"):
        return "wrong_format", f"not_pdf (magic {magic[:5]!r})"
    # Ouvrabilité : pdfinfo. Timeout/rc≠0 => structure cassée (corrompu).
    try:
        info = subprocess.run(["pdfinfo", str(p)], capture_output=True,
                              timeout=timeout, text=True, errors="ignore")
    except subprocess.TimeoutExpired:
        return "corrupt_unreadable", "pdfinfo_timeout"
    except FileNotFoundError:
        info = None  # pdfinfo non installé : on retombe sur pdftotext seul
    except Exception as e:
        return "corrupt_unreadable", f"pdfinfo_error:{type(e).__name__}"
    pages = 0
    if info is not None:
        if info.returncode != 0:
            return "corrupt_unreadable", f"pdfinfo_rc{info.returncode}"
        for line in info.stdout.splitlines():
            if line.startswith("Pages:"):
                try:
                    pages = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pages = 0
    # Densité de texte sur un ÉCHANTILLON des premières pages (PAS tout le document).
    # CORRECTIF 2026-05-20 : lire le doc entier faisait dépasser le timeout sur les
    # gros PDFs (GTTM 372p, Aho 811p), surtout sous scheduling idle/affamé → faux
    # "corrupt_unreadable" en masse. L'échantillon (premières pages) est rapide et
    # indépendant de la taille : un scan a ~0 char en p.1-N, un PDF natif en a beaucoup.
    SAMPLE = 8
    try:
        out = subprocess.run(["pdftotext", "-l", str(SAMPLE), str(p), "-"],
                             capture_output=True, timeout=timeout, text=True, errors="ignore")
    except subprocess.TimeoutExpired:
        # pdfinfo a réussi (doc s'ouvre) mais extraction lente : NE PAS conclure corrompu.
        return "slow_extract", f"pdftotext_timeout on {SAMPLE}p sample (doc s'ouvre)"
    except Exception as e:
        return "corrupt_unreadable", f"pdftotext_error:{type(e).__name__}"
    if out.returncode != 0:
        return "corrupt_unreadable", f"pdftotext_rc{out.returncode}"
    sampled_pages = min(pages, SAMPLE) if pages else SAMPLE
    nchars = len(out.stdout.strip())
    cpp = nchars / sampled_pages
    if cpp < 20:
        return "scan_needs_ocr", f"{cpp:.0f}chars/page over {sampled_pages}p sample"
    return "ok_has_text", f"{cpp:.0f}chars/page over {sampled_pages}p sample ({pages or '?'}p total)"


def detect_off_domain(text: str) -> list[str]:
    """Retourne la liste des catégories hors-domaine détectées (avec match)."""
    text_low = text.lower()
    detected = []
    for category, keywords in OFF_DOMAIN_KEYWORDS.items():
        for kw in keywords:
            if kw in text_low:
                detected.append(f"{category}:{kw}")
                break
    return detected


def detect_on_domain(text: str) -> list[str]:
    """Retourne les keywords on-domain trouvés."""
    text_low = text.lower()
    return [kw for kw in ON_DOMAIN_KEYWORDS if kw in text_low]


def validate_pdf_against_ref(pdf_path: Path, expected_author: str = "",
                              expected_year: str = "", expected_title: str = "",
                              required_title_match: float = 0.0) -> tuple[bool, str]:
    """Valide qu'un PDF correspond à la ref attendue.

    Args:
        pdf_path: chemin du PDF DLed
        expected_author: nom d'auteur attendu (à matcher dans page 1)
        expected_year: année attendue
        expected_title: titre attendu (similarité vs page 1)
        required_title_match: seuil min de similarité titre (0 = pas de check)

    Returns:
        (is_valid, reason). reason est explicite, à utiliser dans le tracking.
    """
    # Sonde santé fichier déterministe (2026-05-20) : route distinctement
    # corrompu / wrong-format / scan / texte. Le code appelant DOIT router :
    #   scan_needs_ocr      → awaiting_rtfm_ocr
    #   corrupt_unreadable  → needs_reacquisition (OCR inutile)
    #   wrong_format        → conversion (epub) ou re-acquisition
    health, detail = probe_pdf_health(pdf_path)
    if health == "missing":
        return False, "pdf_missing"
    if health == "too_small":
        return False, f"pdf_too_small [{detail}]"
    if health == "wrong_format":
        return False, f"pdf_wrong_format [{detail}]"
    if health == "corrupt_unreadable":
        return False, f"pdf_corrupt_unreadable [{detail}] (re-acquérir, PAS d'OCR)"
    if health == "scan_needs_ocr":
        return False, f"pdf_scan_needs_ocr [{detail}] (→ awaiting_rtfm_ocr)"
    if health == "slow_extract":
        # Le doc s'ouvre (pdfinfo OK) mais l'extraction texte est lente : NE PAS
        # conclure corrompu. À revalider hors charge (timeout plus long).
        return False, f"pdf_slow_extract [{detail}] (re-essayer hors charge, PAS corrompu)"
    if health == "ok_epub":
        # EPUB valide : on extrait son texte et on applique les MÊMES contrôles
        # domaine/identité que pour un PDF (un epub valide ≠ bon contenu).
        text = extract_epub_text(pdf_path)
        if len(text) < 50:
            return False, "epub_no_text_extracted"
        off = detect_off_domain(text); on = detect_on_domain(text)
        if off and not on:
            return False, f"epub_bad_match_off_domain {off[:3]} no_on_domain"
        if expected_title:
            distinctive = distinctive_title_words(expected_title)
            tl = text.lower()
            if distinctive and not any(w in tl for w in distinctive):
                return False, f"epub_no_distinctive_title_word (expected ≥1 from {distinctive[:5]})"
        return True, f"validated_epub [on_domain={len(on)} off={len(off)}]"
    # health == ok_has_text
    text = extract_page1(pdf_path)
    if len(text) < 50:
        # page 1 vide mais doc a du texte ailleurs (couverture-image) : lire plus loin
        try:
            out = subprocess.run(["pdftotext", "-l", "6", str(pdf_path), "-"],
                                 capture_output=True, timeout=20, text=True, errors="ignore")
            text = out.stdout[:4000]
        except Exception:
            pass
        if len(text) < 50:
            return False, "pdf_no_text_on_first_pages (cover-only? vérifier manuellement)"

    off = detect_off_domain(text)
    on = detect_on_domain(text)

    # Si off-domain détecté ET pas de on-domain → reject net
    if off and not on:
        return False, f"bad_match_off_domain {off[:3]} no_on_domain_keywords"

    # Si off-domain ET on-domain : ratio pour décider
    if off and len(off) > len(on):
        return False, f"bad_match_majority_off_domain {off[:3]} vs_on={on[:3]}"

    # Check auteur si fourni : doit apparaître quelque part dans page 1
    if expected_author:
        # Premier nom (lastname) de l'auteur
        first_name = expected_author.split()[0] if expected_author.split() else ""
        if len(first_name) >= 3 and first_name.lower() not in text.lower():
            # Auteur pas dans la page 1 — pas un kill switch, mais suspect
            # surtout si pas de on-domain
            if not on:
                return False, f"author_{first_name}_not_in_page1 and no_music_keywords"

    # Check titre si fourni avec seuil
    if expected_title and required_title_match > 0:
        from difflib import SequenceMatcher
        norm = lambda s: re.sub(r"[^\w\s]", " ", (s or "").lower()).strip()
        sim = SequenceMatcher(None, norm(expected_title), norm(text[:500])).ratio()
        if sim < required_title_match:
            # NEW 2026-05-19 : gate secondaire long_word_verbatim_ratio
            # Le seuil SequenceMatcher produit ~36% de faux négatifs car
            # affiliations/headers polluent les 500 premiers chars (mesuré
            # sur batch revalidation post-V3 — Hublet, Bond, Steedman, Vijay-Shanker
            # seraient ratés avec seuil seul).
            # Fallback : si ≥50% des mots ≥5 lettres du titre sont verbatim
            # dans la page 1 entière, on accepte (sans introduire faux positifs
            # car off-domain check déjà passé).
            # MODIF 2026-05-20 : le ratio porte sur les mots DISTINCTIFS uniquement.
            # Avant, un mauvais papier ICM passait car "indian/classical/raga/music"
            # (génériques) suffisaient à atteindre 50%. On exige ≥50% des mots
            # distinctifs ET ≥1 mot distinctif présent.
            distinctive = distinctive_title_words(expected_title)
            if distinctive:
                text_low = text.lower()
                verbatim_hits = [w for w in distinctive if w in text_low]
                verbatim_ratio = len(verbatim_hits) / len(distinctive)
                if verbatim_ratio < 0.50:
                    return False, f"title_similarity_too_low {sim:.2f} and distinctive_word_verbatim {verbatim_ratio:.2f} < 0.50"
                # else : accepté via gate secondaire (≥50% mots distinctifs)
            else:
                return False, f"title_similarity_too_low {sim:.2f} < {required_title_match} (generic-only title)"

    # NEW 2026-05-20 (batch L — ~56% substitutions sur noms indiens courts) :
    # Exiger ≥1 mot DISTINCTIF (≥5 lettres, non générique) du titre verbatim en page 1.
    # Les mots génériques (raga/indian/music/analysis/...) sont partagés par des centaines
    # de papiers ICM/MIR et ne discriminent pas une homonymie — on les exclut.
    # Garde-fou : si le titre n'a QUE des mots génériques (rare), on retombe sur l'ancien
    # check (≥1 mot long quelconque) pour ne pas tout rejeter.
    if expected_title:
        distinctive = distinctive_title_words(expected_title)
        text_low = text.lower()
        if distinctive:
            verbatim_hits = [w for w in distinctive if w in text_low]
            if not verbatim_hits:
                return False, f"no_distinctive_title_word_verbatim (expected ≥1 from {distinctive[:5]})"
        else:
            long_words = [w.lower() for w in re.findall(r"[a-zA-Z]+", expected_title) if len(w) >= 5]
            if long_words and not any(w in text_low for w in long_words):
                return False, f"no_title_long_word_verbatim (generic-only title, expected ≥1 from {long_words[:5]})"

    # NOTE 2026-05-20 : un filtre DUR sur le 1er auteur a été essayé puis RETIRÉ.
    # Le champ `author` du registre est trop souvent dégradé (concaténation issue du
    # slug "ShieberSchabesPereira", acronyme "GTTM", umlauts "Müller"≠"muller") → il
    # produit plus de faux négatifs qu'il ne bloque d'homonymies. La dimension auteur
    # est vérifiée de façon fiable par les agents claim-verify qui LISENT le PDF.
    # Le garde anti-homonymie repose donc sur le mot distinctif du titre (ci-dessus).

    # NEW 2026-05-19 : détecter pattern "Review by:" / "Brief Reviews of Books"
    # — un PDF d'1 page review n'est pas le vrai livre/article
    review_patterns = [
        "review by:", "reviewed by:", "brief reviews of books",
        "book review", "compte rendu de"
    ]
    text_low = text.lower()
    if any(p in text_low for p in review_patterns) and len(text) < 3000:
        return False, "pdf_is_review_not_full_text"

    return True, f"validated [on_domain={len(on)} off={len(off)}]"


def quick_check(pdf_path: Path) -> str:
    """Retourne juste le premier off-domain match (ou empty) pour usage rapide."""
    text = extract_page1(pdf_path)
    off = detect_off_domain(text)
    if off and not detect_on_domain(text):
        return f"OFF_DOMAIN: {off[0]}"
    return ""


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python validate_pdf_content.py <pdf_path> [author] [year] [title]")
        sys.exit(1)
    pdf = Path(sys.argv[1])
    author = sys.argv[2] if len(sys.argv) > 2 else ""
    year = sys.argv[3] if len(sys.argv) > 3 else ""
    title = sys.argv[4] if len(sys.argv) > 4 else ""
    ok, reason = validate_pdf_against_ref(pdf, author, year, title)
    print(f"{'✓ VALID' if ok else '✗ INVALID'}: {reason}")
