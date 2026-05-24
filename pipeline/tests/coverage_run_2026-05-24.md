# Coverage run — Worker B fixes (2026-05-24)

Fichier vivant. Mis à jour à chaque fix avec preuves E2E.
Référence du jeu de test : `coverage_set_2026-05-24.md` (10 slugs gelés).

---

## F3 — `try_archive_org` (étape 5c cascade)

**Statut** : ✅ E2E validé sur 2 refs (1 positif + 1 négatif documenté).

### Test positif — arnold_1982_mathematical_model_shruti (slug #7)

| Phase | Verdict | Détails |
|---|---|---|
| try_archive_org standalone | `success` | identifier=`dli.ministry.16926`, pdf=`JSNA%2867%2929-41.pdf` (5341 KB) |
| _save_and_validate | `success` | `11_Biblio_MIR/Sources/Arnold_1982_A_Mathematical_Model_of_the_Shruti-Swara-Grama-Mur.pdf`, sha256=`9245ec6c24e0...` |
| probe_pdf_health | `ok_has_text` | 2245 chars/page sur 8p, 15p total |
| validate_pdf_against_ref | `True` | `validated [on_domain=7 off=0]` |
| Worker pdf_acquired_dispatch (P4) | `True` | `pdf_acquired → page1_validated via probe_ok_validate_passed` |
| **État final registry** | **`page1_validated`** | uid=`url:https://archive.org/details/dli.ministry.16926` |

### Test négatif — ito_2014_untitled (slug #9)

Livre Springer "Inverse Problems: Tikhonov Theory and Algorithms" — pas sur Internet Archive.

| Phase | Verdict | Détails |
|---|---|---|
| try_archive_org | `no_source` | `ia_no_match_above_threshold`, 5 candidats trouvés via fallback "Ito 2014" mais aucun ne passe le filtre titre ≥ 0.5 + score ≥ 4 |
| Queries tentées | 3 | full title (0 hits), keywords only (0 hits), author+year (5 hits → tous rejetés par best_search_match) |

→ Garde-fou anti-homonymie de `best_search_match` fonctionne : la query large "Ito 2014" retourne 5 résultats, aucun n'est attribué.

### Détails techniques utiles à mémoriser

**Bug encoding IA résolu** : le helper `archive_org_helper.download_public_pdf` ne quote pas les `%` littéraux dans les noms de fichiers IA (ex: `JSNA%2867%2929-41.pdf` contient `%28` `%29` comme caractères du nom). Le wrapper fait `quote(pdf_name, safe='')` avant de construire l'URL. Sinon HTTP 404.

**Stratégie 3-queries** : le helper IA full-text indexing est strict — il ne match pas "Shruti" vs "Shuruti" (transcription variante). Le wrapper enchaîne titre complet → keywords distinctifs → auteur+année (avec `best_search_match` qui filtre les faux positifs en aval).

### Limites identifiées (hors scope F3)

- **Intégration P2** : pour qu'une ref candidate sans DOI passe automatiquement par F3 via `pipeline run`, P2 doit attribuer un uid de fallback (`bibkey:` ou `url:`) quand Crossref/S2 ne donnent rien. Sinon `candidate → uid_resolved` échoue et la cascade ne se déclenche jamais. arnold_1982 a été mis manuellement en `uid:url:...` pour ce test. **À adresser dans un fix séparé ou en extension F1.**
- F3 nécessite un `title` non vide dans la ref. Refs avec `title: null` ou `title: untitled` sortent `no_source` sans tenter.

---

## F1 — Crossref title-fallback strict + bibkey fallback (P2 enrichi)

**Statut** : ✅ E2E validé sur 2 refs (1 fonctionnel + 1 négatif anti-homonymie).

**Modification scope** : P2 (`transitions.candidate_to_uid_resolved`) plutôt que `cascade.try_crossref_oa` comme le plan initial. Raison : la cascade ne reçoit jamais les refs sans DOI tant que P2 ne les a pas migrées de candidate → uid_resolved. Modifier `try_crossref_oa` seul aurait été inopérant.

**Améliorations apportées** :
- Seuil renforcé : `title_similarity ≥ 0.7` (vs 0.6 en V0)
- Filtre auteur strict via `s2_resolver.author_match` 
- Tolérance année ±1 an (vs strict exact)
- Log explicite des candidats rejetés dans `acquisition_attempts[]` (G5)
- **bibkey fallback** : si aucun match strict, attribue `bibkey:auteurAnnéeMot` au lieu de `blocked_by` → la cascade peut tenter F2/F3 ensuite

### Test positif/fonctionnel — collins_2014_biblio_informatique (slug #8)

| Phase | Verdict | Détails |
|---|---|---|
| Crossref title-search | 3 candidats retournés | Live Coding of Consequence (Collins 2011), Live coding teaching SuperCollider (Collins 2016), Live coding laptop performance (Collins 2003) |
| `_pick_crossref_strict` | tous rejetés | sim ∈ {0.542, 0.507, 0.545}, tous < 0.7 |
| S2 fallback | rate-limit 429 | sleep 5s puis pas de match strict |
| Bibkey fallback déclenché | ✓ | uid attribué : `bibkey:collins2014algorave` |
| **État final** | **`uid_resolved`** | cascade peut maintenant tenter F2/F3 sur cette ref |

→ Comportement attendu : le vrai DOI "Algorave" de Collins (papier ICLC 2014, pas indexé Crossref) n'existe pas. Le seuil 0.7 a correctement filtré les papiers similaires-mais-distincts. Bibkey fallback débloque la suite.

### Test négatif — bel_2007_biblio_informatique (slug #10, retracted homonymie)

Script dédié : `pipeline/tests/test_f1_negative.py` (bypass dispatcher car
state=retracted). Le titre du registry est `'11_Biblio_Informatique/Sources/'`
(path parasite — c'est *exactement* le cas où l'ancien matching laxiste
aurait fabriqué un faux DOI).

| Phase | Verdict | Détails |
|---|---|---|
| Crossref title-search | 3 candidats retournés | "Informatique et droit comparé" (1970), "Solutions statiques Schwarzschild" (1971), "Accidental open emerging data sources" (2014) |
| `_pick_crossref_strict` | tous rejetés | sim ∈ {0.542, 0.217, 0.259}, tous < 0.7 ; year_diff ∈ {37, 36, 7} > 1 |
| Test result | **PASS** | aucun DOI attribué → bug P9α v1 non réintroduit |

```
$ venv/bin/python pipeline/tests/test_f1_negative.py
[PASS] F1 a rejeté tous les candidats Crossref (3 rejets) :
  - 10.3406/ridc.1970.15700: sim=0.542, raison=title_sim=0.542<0.7 ; year_diff=37>1
  - 10.1007/bf00759216: sim=0.217, raison=title_sim=0.217<0.7 ; year_diff=36>1
  - 10.1016/j.apgeog.2013.09.012: sim=0.259, raison=title_sim=0.259<0.7 ; year_diff=7>1
→ La ref retracted ne se voit pas réattribuer un DOI. F1 anti-homonymie OK.
```

### Critère G2 F1

```
F1 — testé E2E sur : [collins_2014 (bibkey_fallback OK), bel_2007 (négatif PASS)]
F1 — code écrit non testé E2E : (aucun)
```


---

## F2 — AA title-fallback (étape 7 cascade enrichie)

**Statut** : ✅ extraction MD5 par title-search E2E validée sur 2 refs. ⚠️ DL libgen/library.lol échoue sur les 2 refs testées (sources AA-only nécessitant Playwright/Turnstile, désactivé V1).

**Modification scope** : `cascade.py:try_annas_archive` refactoré avec 3 helpers internes (`_aa_md5_from_doi`, `_aa_md5_from_title`, `_md5_download_cascade`).

**Bug helper plugin détecté** : `lib/annas_archive.AnnasArchive.search_books` retourne actuellement des `BookData` aux champs vides (`url=None`, `mirror=None`, `format=None`, `description=None`). Parser BeautifulSoup cassé ou structure HTML AA modifiée. **À investiguer dans une session séparée** ; pour ne pas bloquer F2, le wrapper fait sa propre extraction depuis `https://annas-archive.gl/search?q=...` via regex split.

**Algorithme** :
1. `re.split(r'/md5/([0-9a-f]{32})', html)` → liste alternée `[head, md5_1, chunk_1, md5_2, chunk_2, …]`
2. Pour chaque chunk : strip HTML tags → texte plain
3. Filtre anti-homonymie : ≥ 1 mot distinctif (≥ 5 lettres) du titre + nom auteur normalisé dans le texte
4. 1ᵉʳ hit qui passe → MD5 retenu

### Test asselin_2000_musique_temperament (slug #6)

| Phase | Verdict | Détails |
|---|---|---|
| `_aa_md5_from_title` | md5 trouvé | `4df2b573fef584db185df1454a4e7bc2`, match keyword='musique' |
| `_md5_download_cascade` | `failed` | `aa_md5_found_but_no_dl` — libgen.li : pas de get.php?key= dans landing ; library.lol : 404 |
| Hypothèse | source AA-only | nécessite Playwright `/slow_download` (sous-fix abandonné en V1 — voir notes) |

### Test garey_johnson_1979_intractability (slug #5, livre ISBN)

| Phase | Verdict | Détails |
|---|---|---|
| `_aa_md5_from_title` | md5 trouvé | `84ff370115d39e89022f3f831a5e8d47`, match keyword='computers' |
| `_md5_download_cascade` | `failed` | idem — sources libgen/library.lol non disponibles pour ce MD5 |

→ Note : garey_johnson_1979 est **déjà en `sota_cited_confirmed`** (PDF déjà acquis ailleurs). Le test démontre que F2 trouve un MD5 plausible mais le DL automatique non-Playwright échoue.

### Sous-fix Playwright (G1 30 min timer — abandonné V1)

Tentative : `pip install playwright` → OK, mais `playwright.sync_api.sync_playwright().chromium.launch()` → `Error: Executable doesn't exist at chrome-headless-shell-linux64/chrome-headless-shell`. Mismatch versions browsers : pip a installé Playwright 1.50 qui demande Chromium 1223, alors que `~/.cache/ms-playwright/` contient 1200 et 1217. `playwright install chromium` aurait DL 5+ min → **abandon** (G1 30 min timer).

**Conséquence** : refs AA-only restent en queue manuelle (~5-10 refs concernées historiquement, gérables au cas par cas).

### Critère G2 F2

```
F2 — testé E2E sur : [asselin_2000 (MD5 trouvé, DL KO documenté), 
                       garey_johnson_1979 (MD5 trouvé, DL KO documenté)]
F2 — code écrit non testé E2E : Playwright sous-fix (mismatch versions, abandonné V1)
```

---

## F4 — WebSearch manifest queue

**Statut** : ✅ E2E validé sur 1 ref + test idempotence.

**Comportement** : append `| slug | query | created_at | status: pending |` au fichier `10_SOURCES/_registry/_websearch_queue.md`. Crée le fichier avec header si absent. Détecte les refs déjà queued (idempotence).

### Test ito_2014_untitled (slug #9)

| Phase | Verdict | Détails |
|---|---|---|
| 1ᵉʳ appel | `no_source` (queued) | Ligne ajoutée : `"Inverse Problems: Tikhonov Theory and Algorithms" "Ito" filetype:pdf` |
| 2ᵉ appel (idempotence) | `no_source` (already_queued) | Ligne **non doublée** |
| Manifest créé | ✓ | header + format table markdown, 1 ligne ajoutée |

### Critère G2 F4

```
F4 — testé E2E sur : [ito_2014 (append OK), ito_2014 (idempotence OK)]
F4 — code écrit non testé E2E : (aucun)
```


---

## F4 — WebSearch = manifest queue (étape 8 cascade)

**Statut** : 🚧 à attaquer.

---

---

## P5 — `reactivate-ocr` via `rtfm check --path` (2026-05-24, post-rescan RTFM)

**Statut** : ✅ E2E validé sur 13 refs réelles `awaiting_rtfm_ocr` (verdicts mixtes documentés).

**Modifications** :
- Nouveau module `pipeline/rtfm_helper.py` : wrapper subprocess + status normalisé.
- Nouvelle transition `awaiting_rtfm_ocr_dispatch` dans `transitions.py`.
- `cli.py:cmd_reactivate_ocr` re-implémenté (était un stub).
- Le helper `rtfm_check_path` accepte exit code 2 (= "no match" valide) en plus de 0.
- Distinguo `still_pending` (fichier sur disque + matches=0 → RTFM pas encore scanné) vs `missing_in_index` (fichier absent → anomalie R8).

### Run sur 13 refs `awaiting_rtfm_ocr`

```
$ venv/bin/python -m pipeline reactivate-ocr

[wait] chomsky_1957_syntactic_structures      rtfm_still_pending
[wait] collins_2003_langages_musicaux         rtfm_still_pending
[wait] degano_1988_untitled                   rtfm_still_pending
[wait] jackendoff_1983_untitled               rtfm_still_pending
[wait] jakobson_1960_linguistics_poetics      rtfm_still_pending
[wait] kleene_1956_biblio_maths               rtfm_still_pending
[wait] lerdahl_2001_biblio_mir                rtfm_missing_in_index  ← anomalie R8
[wait] ludovico_2019_untitled                 rtfm_still_pending
[wait] mani_2005_biblio_maths                 rtfm_still_pending
[wait] mosses_1982_biblio_maths               rtfm_still_pending
[wait] polak_2014_biblio_ethno                rtfm_missing_in_index  ← anomalie R8
[wait] sambamoorthy_1957_untitled             rtfm_still_pending
[wait] weir_1988_untitled                     rtfm_still_pending

# reactivate-ocr — 13 refs en awaiting_rtfm_ocr scannées
  converted                    0
  still_pending               11
  missing_in_index             2
  anomaly                      0
  ocr_failed                   0
  needs_reacq_post_ocr         0
  error                        0
```

### Test du verdict "ok" via helper unit

aaron_2013 (state `sota_cited_confirmed`, déjà acquis, indexé RTFM) :
- `rtfm_status_for_ref('11_Biblio_Informatique/Sources/Aaron_2013_From_Sonic_Pi_to_Overtone.pdf')` → verdict `ok`, chunks=37, embeddings=37

### Anomalies R8 détectées (bonus de P5)

2 refs ont un `pdf_path` doublement préfixé :
- `lerdahl_2001_biblio_mir` : `pdf_path: 10_SOURCES/11_Biblio_MIR/Sources/Lerdahl_2001_Tonal_Pitch_Space_book.pdf` → résout à `.../10_SOURCES/10_SOURCES/...` (n'existe pas)
- `polak_2014_biblio_ethno` : `pdf_path: 10_SOURCES/12_Biblio_Ethno/Sources/Polak_2014_Timing_and_Meter_in_Mande_Drumming.pdf` (même bug)

À corriger manuellement (retirer le préfixe `10_SOURCES/`) ou via un fix R8 séparé.

### Critère G2 P5

```
P5 — testé E2E sur : [chomsky_1957 (still_pending OK), lerdahl_2001 (missing_in_index R8 detected),
                      aaron_2013 (ok via helper unit, chunks=37)]
P5 — code écrit non testé E2E sur refs réelles : ocr_failed (pas de cas dans les 13 actuels),
                      anomaly_zero_chunks (idem), needs_reacq_post_ocr (idem).
                      Ces branches sont vérifiées par lecture du code (chemins explicites,
                      verdicts attribués correctement).
```

---

## Tableau G2 global après tous les fixes

```
F1 — testé E2E sur : [collins_2014 (bibkey_fallback OK), bel_2007 (négatif PASS)]
F1 — code écrit non testé E2E : (aucun)
F2 — testé E2E sur : [asselin_2000 (MD5 trouvé, DL KO doc), garey_johnson_1979 (MD5 trouvé, DL KO doc)]
F2 — code écrit non testé E2E : Playwright sous-fix (mismatch versions, abandonné V1)
F3 — testé E2E sur : [arnold_1982 (positif/success), ito_2014 (négatif/no_match)]
F3 — code écrit non testé E2E : (aucun)
F4 — testé E2E sur : [ito_2014 (append OK), ito_2014 (idempotence OK)]
F4 — code écrit non testé E2E : (aucun)
P5 — testé E2E sur : [chomsky_1957 (still_pending), lerdahl_2001 (missing R8), aaron_2013 (ok unit)]
P5 — code écrit non testé E2E : ocr_failed, anomaly_zero_chunks, needs_reacq_post_ocr (branches sans cas dans les 13 actuels)
```
