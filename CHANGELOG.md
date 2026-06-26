# Changelog

Toutes les modifications notables de ce projet sont documentées dans ce fichier.

Format basé sur [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/).

---

## [1.3.0] - 2026-06-25

### Ajouté

- **Logging détaillé** de chaque appel API et de chaque outil MCP :
  - `[LÉGIFRANCE →]` : méthode, route et payload JSON complet avant chaque appel
  - `[LÉGIFRANCE ←]` : statut HTTP, durée en ms, `totalResultNumber` / nb résultats
  - `[OUTIL]` : nom, arguments (tronqués à 80 chars), aperçu du résultat (120 chars)
  - En cas de 500 : payload complet + message d'erreur de l'API pour diagnostic immédiat

- **`_safe_tool_decorator`** : wrapper global sur les 62 outils — capture toute
  exception et la retourne comme chaîne valide. Empêche la corruption de l'historique
  de conversation Mistral (`"Assistant message must have either content or tool_calls,
  but not none."`) qui bloquait toutes les questions suivantes dans la même session.

- **`_FONDS_VALIDES`** + **`_FOND_ALIASES`** (13 alias) : `CODE`→`CODE_ETAT`,
  `LODA`→`LODA_ETAT`, `JO`→`JORF`, `LOI`→`ALL`, `JURISPRUDENCE`→`JURI`,
  `CONVENTION`→`KALI`, `CIRCULAIRES`→`CIRC`, `ADMINISTRATIF`→`CETAT`, etc.

- **Corrections silencieuses dans `legifrance_rechercher`** (réduit le nombre de
  tours LLM de 5 à 1-2 pour les requêtes mal formées) :
  - `fond='ALL'` + `filtre_facette ≠ 'FOND'` → rebascule sur `fond='JORF'` (seul
    fond compatible), note inline dans le résultat
  - Années dans `query` (ex: `"LPM 2025 2026"`) → extraites vers `date_debut`/
    `date_fin`, query nettoyée, `type_recherche` ajusté — en un seul appel API
  - Alerte inline si `totalResultNumber > 50 000`

- **`_ts_ms_to_date()`** : convertit les timestamps Légifrance en millisecondes
  (ex: `1546819200000`) en date ISO `YYYY-MM-DD`. Gère la valeur sentinelle `2999`.

- **`_fmt_juri()`** : formateur unifié pour toutes les décisions de jurisprudence.
  Expose : `solution`, `sommaire`, `motsCles`, `numeroAffaire`, `formation`,
  `publicationRecueil`, `ecli`, `juridictionJudiciaire`, `natureJuridiction`, date
  convertie. Paramètre `avec_texte_integral` (défaut : `False`).

- **`CODE_IDS` complet** : 89 entrées (74 LEGITEXT distincts) couvrant les codes
  officiels DILA. Ajouts : commande publique, juridictions financières, procédures
  d'exécution, justice militaire et pénale des mineurs, code des impositions sur les
  biens et services, CGI annexes I-IV, douanes, mutualité, 3 régimes de pensions,
  ports maritimes, forestier, cinéma, artisanat, voirie routière, domaine de l'État,
  expropriation, travail maritime, discipline marine marchande, service national,
  légion d'honneur, communes (et Nouvelle-Calédonie), minier nouveau/ancien, rural
  ancien. Alias : `cgfp`, `cgi`, `cja`, `crpa`, `cgct`, `ceseda`, `g3p`, `lpf`.

- **`resolve_code()`** : normalisation Unicode, suppression du préfixe
  "code (de la/du/des/de l')", correspondance partielle et inverse. Retourne une
  chaîne d'erreur avec liste des codes disponibles (plus de `ValueError`).

- **Variantes de numéros d'article** dans `legifrance_article_par_numero` :
  essaie `L52` → `52`, `52` → `L52` → `R52` → `52`. Couvre les codes à préfixe
  législatif/réglementaire (CGFP, CJA, CGCT…).

### Corrigé

- **4 LEGITEXT erronés** dans `CODE_IDS` :

  | Code | Ancien ID (faux) | ID officiel |
  |---|---|---|
  | Code pénitentiaire | `000043395876` | `000045476241` |
  | Code de l'aviation civile | `000006072301` | `000006074234` |
  | Code rural et de la pêche maritime | `000006071367` (= rural *ancien*) | `000022197698` |
  | `"deontologie"` | `000006070299` (= Code général de la propriété des personnes publiques) | supprimé ; `deontologie_architectes`→`000006074232`, `g3p`→`000006070299` |

- **1 LEGITEXT fictif** supprimé : `famille`→`000006070155` inexistant → remplacé
  par `famille_aide_sociale`→`000006072637`.

- **`_req` : no retry sur 500** — un 500 PISTE signifie "requête invalide", pas
  une erreur transitoire. Seuls 502/503/504 déclenchent un retry.

- **`filtre_valeur` → `filtre_valeurs`** (tableau) — `FiltreDTO` n'a pas de champ
  `valeur`. Le champ correct est `valeurs` (array). Tous les filtres non-date
  étaient silencieusement ignorés par l'API depuis l'origine.

- **`legifrance_article_par_numero`**, **`legifrance_consulter_code`**,
  **`legifrance_code_complet`** : suppression des `try/except ValueError` morts —
  sans cette correction, les codes non reconnus envoyaient la chaîne d'erreur
  comme `id` à l'API → 500.

- **`fond='ALL'` + `filtre_facette ≠ 'FOND'`** → 500 systématique (désormais
  rebascule silencieusement sur `fond='JORF'`).

- **Années dans `query`** avec `UN_DES_MOTS` → millions de résultats (désormais
  extraites automatiquement vers `date_debut`/`date_fin`).

- **`NOM_CODE`** retiré des filtres recommandés : produit systématiquement un 500
  avec du texte libre (valeurs indexées non documentées).

- **`legifrance_jurisprudence`** : `dateTexte` (timestamp ms) était affiché brut ;
  9 champs essentiels manquants (`solution`, `sommaire`, `motsCles`, `formation`,
  `ecli`, `numeroAffaire`, `publicationRecueil`, `juridictionJudiciaire`,
  `natureJuridiction`). Outil entièrement reécrit avec `_fmt_juri`.

- **`_fmt_search`** : champ `datePubliTexte` (inexistant dans `SearchTitle`)
  remplacé par `dateSignature`/`datePublication` (sur `SearchResult`).

### Amélioré

- Resources `fonds`, `filtres-facettes`, `workflow-recherche`, `codes-disponibles`
  mises à jour avec les pièges documentés (fond=ALL+filtre, années dans query,
  NOM_CODE, corrections silencieuses, 79 codes avec corrections d'IDs).

---

## [1.2.0] - 2026-06-23


### Ajouté

- **11 MCP Resources** de documentation injectées dans le contexte du LLM avant chaque appel :
  `legifrance://documentation/fonds`, `/champs-recherche`, `/types-recherche`, `/options-tri`,
  `/filtres-dates`, `/filtres-facettes`, `/identifiants`, `/codes-disponibles`,
  `/workflow-recherche`, `/etats-juridiques`, `/pagination-chrono`.
  Ces ressources documentent les fonds, champs (`typeChamp`), types de recherche, critères de
  tri par fond, filtres de date et facettes, formats d'identifiants, codes disponibles, et les
  workflows recommandés.

- **`legifrance_rechercher` — 3 nouveaux paramètres** :
  - `type_recherche` : logique de correspondance sur la requête — `EXACTE`,
    `TOUS_LES_MOTS_DANS_UN_CHAMP`, `UN_DES_MOTS` (défaut), `AUCUN_DES_MOTS`,
    `AUCUNE_CORRESPONDANCE_A_CETTE_EXPRESSION`. Permet des recherches exactes (termes
    juridiques figés) ou d'exclusion.
  - `operateur` : opérateur logique global entre champs — `ET` (défaut) ou `OU`.
  - `second_tri` : critère de tri secondaire en cas d'égalité (`secondSort`), mêmes valeurs
    que `tri`.

- **`legifrance_rechercher` — paramètre `champ`** (typeChamp) : ciblage d'un champ précis
  selon le fond (TITLE, NOR, ARTICLE, IDCC, ABSTRATS, NUM_DEC, RAISON_SOCIALE…).

- **`legifrance_rechercher` — filtres de date** : `date_debut` / `date_fin` désormais
  effectivement transmis à l'API via la facette de date propre à chaque fond (table
  `_DATE_FACETTE_PAR_FOND` extraite du fichier de description des tris et filtres DILA).

- **`legifrance_rechercher` — filtre par facette** : paramètre `filtre_facette` +
  `filtre_valeurs` (liste) pour filtrer par nature (DECRET, LOI…), état juridique
  (VIGUEUR, ABROGE…), NOR, JURIDICTION, IDCC et toute autre facette documentée.

- **Tables de référence** issues du fichier de description des tris et filtres DILA :
  - `_DATE_FACETTE_PAR_FOND` — facette de date par fond
  - `_CHAMP_PAR_FOND` — valeurs `typeChamp` valides par fond
  - `_SORT_PAR_FOND` — critères de tri valides par fond (validation automatique avec repli
    silencieux sur `PERTINENCE` en cas de valeur invalide)

- **`legifrance_consulter_code`** : nouveau paramètre `nature` transmis à
  `/consult/legi/tableMatieres`.

- **`legifrance_jurisprudence_plan_classement`** : 4 nouveaux paramètres — `juri_id`, `page`,
  `search_suggest`, `search_by_niveau` — conformément au schéma de l'API.

- **`legifrance_lister_debats_parlementaires`** : paramètre `tri`
  (`DEBAT_PARLEMENTAIRE_DESC/ASC`, `ID_ASC/DESC`) et filtre `facetteTypePublication`.

- **`legifrance_lister_questions_parlementaires`** : paramètre `tri`
  (`QUESTION_ECRITE_PARLEMENTAIRE_DESC/ASC`, `ID_ASC/DESC`).

- **`legifrance_lister_bocc`** : paramètre `tri` (`BOCC_SORT_DESC/ASC`) et filtre `idcc`.

- **`legifrance_lister_docs_admins`** : paramètre `nb_resultats`.

- **Helpers internes** :
  - `_fmt_texte_integral()` : formateur récursif sections/articles pour les endpoints
    retournant `ConsultTextResponse` complet.
  - `_fmt_liens_list()` : formateur pour les listes de liens légers (`ArticleDTO`,
    `LiensRelatifsDTO`).

### Corrigé

- **12 routes inexistantes** (renvoyaient systématiquement 404) corrigées :

  | Outil | Ancienne route (fausse) | Nouvelle route (swagger) |
  |---|---|---|
  | `legifrance_code_par_ancien_id` | `/consult/legi/tableMatieresByOldId` | `/consult/getCodeWithAncienId` |
  | `legifrance_article_par_eli` | `/consult/getArticleByEli` | `/consult/getArticleWithIdEliOrAlias` |
  | `legifrance_articles_meme_numero` | `/consult/getArticlesWithSameNum` | `/consult/sameNumArticle` |
  | `legifrance_legi_part` | `/consult/legi/getLegiPart` | `/consult/legiPart` |
  | `legifrance_liens_concordance` | `/consult/concordance` | `/consult/concordanceLinksArticle` |
  | `legifrance_liens_relatifs` | `/consult/relatifs` | `/consult/relatedLinksArticle` |
  | `legifrance_liens_service_public` | `/consult/getLinksServicePublic` | `/consult/servicePublicLinksArticle` |
  | `legifrance_a_liens_service_public` | `/consult/hasLinksServicePublic` | `/consult/hasServicePublicLinksArticle` |
  | `legifrance_eli_alias_texte` | `/consult/getEliAlias` | `/consult/eliAndAliasRedirectionTexte` |
  | `legifrance_jurisprudence_ancien_id` | `/consult/getJuriByOldId` | `/consult/getJuriWithAncienId` |
  | `legifrance_cnil_ancien_id` | `/consult/getCnilByOldId` | `/consult/getCnilWithAncienId` |
  | `legifrance_lister_questions_parlementaires` | `/list/questionsParlementaires` | `/list/questionsEcritesParlementaires` |

- **Champs de requête erronés** corrigés sur les 12 routes ci-dessus :
  `oldId` → `ancienId`, `eli` → `idEliOrAlias`, `id` → `articleId` / `articleCid` / `textId`,
  `parlement` → `parlementTypes`, et le body de `hasServicePublicLinksArticle` est désormais
  un tableau JSON brut `["cid"]` (pas un objet).

- **`legifrance_articles_meme_numero`** : outil entièrement redessiné — le schéma
  `SameNumArticleRequest` exige `textCid`, `articleCid`, `articleNum`, `date` (tous
  obligatoires). Signature de l'outil mise à jour en conséquence.

- **`legifrance_dates_sans_jo`** : méthode `POST` → `GET` sans body. Clé de réponse corrigée :
  `dates`/`datesWithoutJo` → `lstDateDisabled`. Paramètre `annee` rendu optionnel (filtrage
  local). L'API retourne toutes les dates sans filtre serveur.

- **`legifrance_annees_sans_table`** : méthode `POST` → `GET` sans body. Clé de réponse
  corrigée : `years`/`annees` → `lstYearDisabled`.

- **`legifrance_lister_bodmr`** : champ de filtre `year` (entier) → `years` (tableau)
  conformément au schéma `BodmrListRequest`.

- **`legifrance_lister_docs_admins`** : clé de filtre `years` → `facetteYearPubli`
  (nom réel de la facette dans l'API).

- **`legifrance_code_complet`** : appelait `/consult/legi/tableMatieres` (table des matières
  uniquement) et formatait avec `_fmt_toc`, ne retournant jamais le texte des articles malgré
  le docstring. Désormais appelle `/consult/code` et utilise `_fmt_texte_integral`.

- **`legifrance_obtenir_article`**, **`legifrance_article_par_numero`**,
  **`legifrance_article_par_eli`** : désenveloppement du wrapper `GetArticleResponse`
  (`{"article": {...}}`) avant formatage — le texte des articles n'était jamais retourné.

- **`legifrance_rechercher`** : filtre non-date corrigé — le champ s'appelle `valeurs`
  (tableau) dans `FiltreDTO`, et non `valeur` (chaîne). Les filtres par NATURE, état
  juridique, etc. étaient silencieusement ignorés par l'API. Paramètre renommé
  `filtre_valeurs` (liste) pour refléter ce comportement.

- **`legifrance_legi_part`** : paramètre `date` rendu obligatoire (défaut : aujourd'hui)
  conformément au schéma `LegiPartRequest`.

- **`legifrance_jurisprudence_ancien_id`** et **`legifrance_cnil_ancien_id`** : désenveloppement
  du wrapper `{text: {...}}` avant formatage.

- **`legifrance_lister_debats_parlementaires`** : clé de filtre `typesPublication` →
  `facetteTypePublication`.

### Amélioré

- **`legifrance_sommaire_jorf`** : docstring enrichie d'un avertissement explicite — cet outil
  liste les textes publiés dans le JO par date et ne fait pas de recherche par contenu ou
  thème. Redirige vers `legifrance_rechercher` pour les recherches thématiques.

- **`legifrance_code_par_ancien_id`** : utilise désormais `_fmt_texte_integral` (contenu
  complet avec articles) au lieu de `_fmt_toc` (table des matières uniquement).

- **`legifrance_legi_part`** : utilise désormais `_fmt_texte_integral` au lieu d'un
  formateur ad hoc partiel.

- **`legifrance_eli_alias_texte`** : retourne désormais le contenu complet du texte
  (nature, date, articles) au lieu d'un simple CID.

- **`legifrance_jurisprudence_ancien_id`** et **`legifrance_cnil_ancien_id`** : retournent
  désormais le texte intégral de la décision.

- **`legifrance_liens_concordance`** et **`legifrance_liens_relatifs`** : réponses
  reformatées selon les champs réels (`newTexts`/`oldTexts`, `liensCite`/`liensCitePar`).

- **`_req()`** : paramètre `body` élargi de `Dict` à `Any` pour supporter les endpoints
  dont le body est un tableau JSON brut (`hasServicePublicLinksArticle`).

---

## [1.1.0] - 2026-05-26

### Ajouté

- **Dockerfile** : image Docker officielle basée sur `python:3.12-slim`. Permet de déployer le serveur MCP sans installer Python localement, sur n'importe quelle machine accessible en réseau.
- **README** : nouvelle section *Installation via Docker* et section *Démarrage via Docker* avec les commandes `docker build` et `docker run`.
- **README** : note de compatibilité des identifiants PISTE avec `mcp-judilibre`.
- **README** : nouvelle section *Utilisation combinée avec mcp-judilibre* et exemple de configuration Claude Desktop pour les deux serveurs simultanément.

---

## [1.0.0] - 2026-05-26

### Ajouté

- Serveur MCP HTTP Streamable (spec 2025-03-26) exposant l'intégralité de l'API Légifrance (PISTE / DILA).
- 62 outils MCP répartis en 8 domaines :
  - **JORF** — Journal Officiel (7 outils)
  - **LEGI** — Codes législatifs (20 outils)
  - **LODA** — Lois et décrets (5 outils)
  - **KALI** — Conventions collectives (6 outils)
  - **Jurisprudence** — CASS, CETAT, CONSTIT, JURITEXT (3 outils)
  - **Parlementaire** — Dossiers, débats, questions (7 outils)
  - **Documents administratifs** — CIRC, ACCO, CNIL, BOCC, BODMR (10 outils)
  - **Recherche et utilitaires** — Recherche plein texte, suggestions, version (5 outils)
- Authentification OAuth2 client credentials via la plateforme PISTE, avec renouvellement automatique du token et 3 tentatives en cas d'erreur réseau.
- Chargement optionnel des identifiants via fichier `.env` (python-dotenv).
- Compatible Claude Desktop, Claude Code, Demeter, et tout client MCP supportant HTTP Streamable.
