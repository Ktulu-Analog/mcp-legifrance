#!/usr/bin/env python3
# ============================================================================
# Légifrance — Serveur MCP HTTP Streamable (spec 2025-03-26)
# ============================================================================
# Auteur  : Pierre COUGET  ktulu.analog@gmail.com
# Licence : GNU Affero General Public License v3.0 (AGPL-3.0)
#           https://www.gnu.org/licenses/agpl-3.0.html
# Année   : 2026
# ----------------------------------------------------------------------------
# Ce fichier expose les 62 outils Légifrance via le protocole MCP
# HTTP Streamable (spec 2025-03-26), utilisable par n'importe quel client MCP.
#
# Démarrage :
#   LEGIFRANCE_CLIENT_ID=... LEGIFRANCE_CLIENT_SECRET=... python server.py
#
# Ou via un fichier .env :
#   pip install python-dotenv
#   (le serveur charge automatiquement .env s'il est présent)
#
# L'endpoint MCP est accessible à :
#   http://localhost:6502/mcp
# ============================================================================

import logging
import os
import re
import time
from datetime import date as _date_today
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

# Chargement optionnel du .env (python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Création du serveur MCP
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="legifrance",
    instructions=(
        "Serveur MCP donnant accès à l'API Légifrance (PISTE). "
        "62 outils couvrant : recherche fulltext, codes juridiques, lois/décrets, "
        "Journal Officiel, conventions collectives (KALI), jurisprudence, CNIL, "
        "accords d'entreprise, circulaires, débats parlementaires, dossiers législatifs, "
        "BOCC et historique des versions. "
        "Prérequis : variables d'environnement LEGIFRANCE_CLIENT_ID et LEGIFRANCE_CLIENT_SECRET."
    ),
)

# ---------------------------------------------------------------------------
# Client Légifrance (OAuth2 PISTE, singleton)
# ---------------------------------------------------------------------------

class _LegifranceClient:
    """Client OAuth2 PISTE — singleton, partagé par tous les outils MCP."""

    PRODUCTION_BASE_URL = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
    OAUTH_URL           = "https://oauth.piste.gouv.fr/api/oauth/token"

    CODE_IDS: Dict[str, str] = {
        "civil":                    "LEGITEXT000006070721",
        "penal":                    "LEGITEXT000006070719",
        "travail":                  "LEGITEXT000006072050",
        "commerce":                 "LEGITEXT000005634379",
        "consommation":             "LEGITEXT000006069565",
        "procedure_civile":         "LEGITEXT000006070716",
        "procedure_penale":         "LEGITEXT000006071154",
        "sante_publique":           "LEGITEXT000006072665",
        "education":                "LEGITEXT000006071191",
        "environnement":            "LEGITEXT000006074220",
        "securite_sociale":         "LEGITEXT000006073189",
        "general_collectivites":    "LEGITEXT000006070633",
        "urbanisme":                "LEGITEXT000006074075",
        "propriete_intellectuelle": "LEGITEXT000006069414",
        "assurances":               "LEGITEXT000006073984",
        "monetaire_financier":      "LEGITEXT000006072026",
    }

    def __init__(self):
        self._token: Optional[str] = None
        self._token_expiry: float  = 0
        self._http = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )

    def _get_token(self) -> str:
        client_id     = os.getenv("LEGIFRANCE_CLIENT_ID", "").strip()
        client_secret = os.getenv("LEGIFRANCE_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise RuntimeError(
                "Identifiants Légifrance manquants. "
                "Définissez les variables d'environnement "
                "LEGIFRANCE_CLIENT_ID et LEGIFRANCE_CLIENT_SECRET "
                "(obtenez-les sur https://piste.gouv.fr)."
            )
        if self._token and time.time() < (self._token_expiry - 60):
            return self._token
        r = self._http.post(
            self.OAUTH_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
                "scope":         "openid",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        d = r.json()
        self._token        = d["access_token"]
        self._token_expiry = time.time() + d.get("expires_in", 3600)
        return self._token

    def _req(self, path: str, method: str = "POST", body: Optional[Dict] = None) -> Dict:
        url = f"{self.PRODUCTION_BASE_URL}{path}"
        for attempt in range(3):
            try:
                resp = self._http.request(
                    method=method.upper(),
                    url=url,
                    headers={
                        "Authorization": f"Bearer {self._get_token()}",
                        "Accept":        "application/json",
                        "Content-Type":  "application/json",
                    },
                    json=body,
                )
                if resp.status_code == 401 and attempt == 0:
                    self._token = None
                    continue
                if resp.status_code == 429:
                    raise RuntimeError("Limite de débit API Légifrance atteinte (429)")
                resp.raise_for_status()
                ct = resp.headers.get("Content-Type", "")
                return resp.json() if "application/json" in ct else {"content": resp.text}
            except httpx.HTTPStatusError as e:
                if attempt == 2 or e.response.status_code < 500:
                    raise RuntimeError(f"Erreur HTTP {e.response.status_code} sur {path}")
                time.sleep(2 ** attempt)
            except httpx.RequestError as e:
                if attempt == 2:
                    raise RuntimeError(f"Erreur réseau : {e}")
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Échec après 3 tentatives sur {path}")

    def resolve_code(self, code: str) -> str:
        if code.startswith("LEGITEXT"):
            return code
        key = code.lower().replace(" ", "_").replace("-", "_")
        rid = self.CODE_IDS.get(key)
        if not rid:
            known = ", ".join(self.CODE_IDS.keys())
            raise ValueError(
                f"Code non reconnu : '{code}'.\n"
                f"Noms valides : {known}\n"
                f"Ou utilisez directement un ID LEGITEXT..."
            )
        return rid


_client: Optional[_LegifranceClient] = None

def _get_client() -> _LegifranceClient:
    global _client
    if _client is None:
        _client = _LegifranceClient()
    return _client


# ---------------------------------------------------------------------------
# Utilitaires de formatage Markdown
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<p[^>]*>", "\n", text)
    text = re.sub(r"</p>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _fmt_date(d) -> str:
    """Convertit une date en string lisible.
    Accepte : string ISO, timestamp Unix en ms (int), ou None."""
    if not d:
        return ""
    if isinstance(d, int):
        # timestamp ms → date ISO
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(d / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return str(d)
    return str(d)


def _fmt_article(a: Dict) -> str:
    if not a:
        return ""
    num    = a.get("num",    a.get("numero", a.get("number", "")))
    titre  = a.get("titre",  a.get("title",  ""))
    texte  = _strip_html(a.get("texteHtml", a.get("texte", a.get("content", ""))))
    rid    = a.get("id",     "")
    etat   = a.get("etat",   a.get("legalStatus", ""))
    debut  = a.get("dateDebut", a.get("dateVigueur", ""))
    lines  = []
    header = f"### Article {num}" if num else "### Article"
    if titre:
        header += f" — {titre}"
    lines.append(header)
    if etat:
        lines.append(f"*État : {etat}*")
    if debut:
        lines.append(f"*En vigueur depuis : {debut}*")
    if rid:
        lines.append(f"*ID : `{rid}`*")
    if texte:
        lines.append(f"\n{texte[:3000]}")
        if len(texte) > 3000:
            lines.append("\n*[Texte tronqué à 3000 caractères]*")
    return "\n".join(lines)


def _fmt_search(data: Dict, query: str) -> str:
    results = data.get("results", [])
    total   = data.get("totalResultNumber", 0)
    if not results:
        return f"Aucun résultat pour : « {query} »"
    lines = [f"{total} résultat(s) — {len(results)} affichés\n"]
    for i, r in enumerate(results, 1):
        t_list = r.get("titles", [{}])
        title  = t_list[0].get("title", r.get("title", "Sans titre"))
        # L'API retourne 2 champs distincts dans SearchTitle :
        #   id  = identifiant de version  (ex: JORFARTI000053598802)
        #   cid = Chronical ID à passer à /consult/jorf (ex: JORFTEXT000053598801)
        rid    = t_list[0].get("cid",    t_list[0].get("id", r.get("cid", r.get("id", ""))))
        date_p = t_list[0].get("datePubliTexte", "")
        title  = title[:120] + ("…" if len(title) > 120 else "")
        lines.append(f"**{i}.** {title}")
        if date_p:
            lines.append(f"   Date : {date_p}")
        if rid:
            lines.append(f"   ID : `{rid}`")
        lines.append("")
    return "\n".join(lines)


def _fmt_toc(data: Dict, code: str) -> str:
    lines = [f"# Table des matières — {code}\n"]

    def walk(items, depth=0):
        indent = "  " * depth
        for item in items:
            t   = item.get("titre", item.get("title", ""))
            rid = item.get("id", item.get("cid", ""))
            if t:
                if depth == 0:
                    lines.append(f"\n## {t}")
                else:
                    lines.append(f"{indent}- {t}" + (f" (`{rid}`)" if rid else ""))
            children = item.get("sections", item.get("articles", item.get("children", [])))
            if children and depth < 3:
                walk(children, depth + 1)

    walk(data.get("sections", data.get("articles", [])))
    return "\n".join(lines) if len(lines) > 1 else f"Structure vide pour {code}."


# ===========================================================================
# OUTILS MCP — Recherche (4)
# ===========================================================================

@mcp.tool()
def legifrance_rechercher(
    query: str,
    nb_resultats: int = 5,
    fond: str = "ALL",
    date_debut: Optional[str] = None,
    date_fin: Optional[str] = None,
    tri: str = "PERTINENCE",
) -> str:
    """Recherche de textes juridiques français dans Légifrance.

    Interroge codes, lois, décrets, arrêtés, circulaires, jurisprudence,
    conventions collectives et Journal Officiel. Retourne titres, natures et identifiants.

    Args:
        query: Mots-clés (ex: 'responsabilité civile', 'licenciement abusif', 'RGPD').
        nb_resultats: Nombre de résultats (1–10, défaut: 5).
        fond: Fonds à interroger. ALL=tous. CODE_DATE, CODE_ETAT, LODA_DATE, LODA_ETAT,
              JURI, JUFI, CETAT, CONSTIT, JORF, KALI, CNIL, CIRC, ACCO.
        date_debut: Date minimale de publication YYYY-MM-DD.
        date_fin: Date maximale de publication YYYY-MM-DD.
        tri: PERTINENCE (défaut) ou SIGNATURE_DATE_DESC.
    """
    c = _get_client()
    try:
        query = query.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    recherche: Dict[str, Any] = {
        "champs": [{"typeChamp": "ALL",
                    "criteres": [{"typeRecherche": "UN_DES_MOTS", "valeur": query, "operateur": "ET"}],
                    "operateur": "ET"}],
        "operateur":      "ET",
        "pageSize":       min(max(1, nb_resultats), 20),
        "pageNumber":     1,
        "sort":           tri,
        "typePagination": "DEFAUT",
    }
    data = c._req("/search", body={"fond": fond, "recherche": recherche})
    return _fmt_search(data, query)


@mcp.tool()
def legifrance_version_canonique_article(article_id: str) -> str:
    """Récupère les informations de version canonique d'un article.

    Utile pour résoudre l'identifiant stable (CID) d'un article versionnée.

    Args:
        article_id: Identifiant de l'article (ex: LEGIARTI000006436298).
    """
    c    = _get_client()
    data = c._req("/search/canonicalArticleVersion", body={"id": article_id})
    lines = [f"**Version canonique de l'article `{article_id}`**\n"]
    for k, v in data.items():
        if v:
            lines.append(f"- **{k}** : {v}")
    return "\n".join(lines) if len(lines) > 1 else f"Aucune information pour `{article_id}`."


@mcp.tool()
def legifrance_version_canonique(cid_text: str, date: str, cid_section: Optional[str] = None) -> str:
    """Récupère la version canonique d'un texte à une date donnée.

    Permet de déterminer quelle version d'un texte était en vigueur à une date précise.

    Args:
        cid_text: CID du texte (ex: LEGITEXT000006070721).
        date: Date de référence YYYY-MM-DD.
        cid_section: CID de la section (optionnel).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"cidText": cid_text, "date": date}
    if cid_section:
        body["cidSection"] = cid_section
    data  = c._req("/search/canonicalVersion", body=body)
    lines = [f"**Version canonique de `{cid_text}` au {date}**\n"]
    for k, v in data.items():
        if v:
            lines.append(f"- **{k}** : {v}")
    return "\n".join(lines) if len(lines) > 1 else "Aucune information de version canonique trouvée."


@mcp.tool()
def legifrance_version_proche(cid_text: str, date: str, cid_section: Optional[str] = None) -> str:
    """Récupère la version d'un texte la plus proche d'une date donnée.

    Utile quand la date exacte n'a pas de version et qu'on veut la version voisine.

    Args:
        cid_text: CID du texte (ex: LEGITEXT000006070721).
        date: Date de référence YYYY-MM-DD.
        cid_section: CID de la section (optionnel).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"cidText": cid_text, "date": date}
    if cid_section:
        body["cidSection"] = cid_section
    data  = c._req("/search/nearestVersion", body=body)
    lines = [f"**Version la plus proche de `{cid_text}` autour du {date}**\n"]
    for k, v in data.items():
        if v:
            lines.append(f"- **{k}** : {v}")
    return "\n".join(lines) if len(lines) > 1 else "Aucune version proche trouvée."


# ===========================================================================
# OUTILS MCP — Consultation codes / lois (11)
# ===========================================================================

@mcp.tool()
def legifrance_consulter_code(code: str, date: Optional[str] = None) -> str:
    """Consulte la table des matières structurée d'un code juridique français.

    Retourne la hiérarchie des parties, livres, titres et articles avec leurs identifiants.
    Utilisez legifrance_obtenir_article ou legifrance_article_par_numero pour lire un article précis.

    Args:
        code: Nom du code (civil, penal, travail, commerce, consommation,
              procedure_civile, procedure_penale, sante_publique, education,
              environnement, securite_sociale, urbanisme, assurances,
              monetaire_financier, propriete_intellectuelle)
              ou directement un ID Légifrance (LEGITEXT...).
        date: Date de consultation YYYY-MM-DD (défaut: aujourd'hui).
    """
    c = _get_client()
    try:
        tid = c.resolve_code(code)
    except ValueError as e:
        return str(e)
    data = c._req("/consult/legi/tableMatieres", body={
        "textId": tid,
        "date":   _date_today.today().isoformat() if date is None else date,
    })
    return _fmt_toc(data, code)


@mcp.tool()
def legifrance_code_complet(code: str, date: Optional[str] = None) -> str:
    """Récupère le contenu complet d'un code juridique avec tous ses articles.

    Attention : réponse potentiellement volumineuse. Préférez legifrance_consulter_code
    puis legifrance_obtenir_article pour une navigation ciblée.

    Args:
        code: Nom ou ID LEGITEXT du code.
        date: Date de consultation YYYY-MM-DD (défaut: aujourd'hui).
    """
    c = _get_client()
    try:
        tid = c.resolve_code(code)
    except ValueError as e:
        return str(e)
    data = c._req("/consult/legi/tableMatieres", body={
        "textId":    tid,
        "date":      _date_today.today().isoformat() if date is None else date,
        "withLinks": True,
    })
    return _fmt_toc(data, code)


@mcp.tool()
def legifrance_code_par_ancien_id(ancien_id: str, date: Optional[str] = None) -> str:
    """Consulte un code par son ancien identifiant Légifrance.

    Utile pour résoudre des références héritées vers des identifiants actuels.

    Args:
        ancien_id: Ancien identifiant du code.
        date: Date de consultation YYYY-MM-DD (défaut: aujourd'hui).
    """
    c    = _get_client()
    data = c._req("/consult/legi/tableMatieresByOldId", body={
        "oldId": ancien_id,
        "date":  _date_today.today().isoformat() if date is None else date,
    })
    return _fmt_toc(data, ancien_id)


@mcp.tool()
def legifrance_obtenir_article(article_id: str) -> str:
    """Récupère le texte complet d'un article juridique par son identifiant Légifrance.

    L'ID (format LEGIARTI000...) s'obtient via legifrance_rechercher ou legifrance_consulter_code.

    Args:
        article_id: Identifiant Légifrance de l'article (ex: LEGIARTI000006419283).
    """
    c    = _get_client()
    data = c._req("/consult/getArticle", body={"id": article_id})
    return _fmt_article(data)


@mcp.tool()
def legifrance_article_par_numero(code: str, numero: str) -> str:
    """Récupère un article d'un code par son numéro (ex: '1382', 'L1234-5', 'R123-1').

    Plus pratique que legifrance_obtenir_article quand on connaît le numéro mais pas l'ID.

    Args:
        code: Nom du code (civil, travail…) ou ID Légifrance (LEGITEXT...).
        numero: Numéro de l'article (ex: '1382', 'L1234-5', 'R10').
    """
    c = _get_client()
    try:
        tid = c.resolve_code(code)
    except ValueError as e:
        return str(e)
    data = c._req("/consult/getArticleWithIdAndNum", body={"id": tid, "num": numero})
    return _fmt_article(data)


@mcp.tool()
def legifrance_article_par_eli(eli: str) -> str:
    """Récupère un article par son identifiant ELI (European Legislation Identifier).

    L'ELI est le format d'URI standardisé européen pour identifier les textes législatifs
    (ex: eli/loi/2016/10/7/2016-1321/jo/texte).

    Args:
        eli: Identifiant ELI de l'article.
    """
    c    = _get_client()
    data = c._req("/consult/getArticleByEli", body={"eli": eli})
    return _fmt_article(data)


@mcp.tool()
def legifrance_versions_article(article_cid: str) -> str:
    """Récupère l'historique complet des versions d'un article.

    Permet de suivre les modifications législatives d'un article au fil du temps.
    Utilise le CID (identifiant canonique) de l'article.

    Args:
        article_cid: CID (identifiant canonique) de l'article.
    """
    c        = _get_client()
    data     = c._req("/consult/getArticleByCid", body={"cid": article_cid})
    versions = data.get("listArticle", [])
    if not versions:
        return f"Aucune version trouvée pour le CID `{article_cid}`."
    lines = [f"**{len(versions)} version(s) de l'article `{article_cid}`**\n"]
    for v in versions:
        lines.append(_fmt_article(v))
        lines.append("\n---\n")
    return "\n".join(lines)


@mcp.tool()
def legifrance_articles_meme_numero(code: str, numero: str) -> str:
    """Récupère tous les articles portant le même numéro dans un code.

    Utile pour les articles qui ont été renumérotés ou qui existent en plusieurs versions.

    Args:
        code: Nom ou ID LEGITEXT du code.
        numero: Numéro de l'article.
    """
    c = _get_client()
    try:
        tid = c.resolve_code(code)
    except ValueError as e:
        return str(e)
    data     = c._req("/consult/getArticlesWithSameNum", body={"id": tid, "num": numero})
    articles = data.get("listArticle", data.get("articles", []))
    if not articles:
        return f"Aucun article numéroté '{numero}' dans ce code."
    lines = [f"**{len(articles)} article(s) portant le numéro '{numero}'**\n"]
    for a in articles:
        lines.append(_fmt_article(a))
        lines.append("---")
    return "\n".join(lines)


@mcp.tool()
def legifrance_loi_decret(text_id: str, date: Optional[str] = None) -> str:
    """Consulte le contenu complet d'une loi, décret, ordonnance ou arrêté (fonds LODA).

    Retourne les articles structurés du texte.

    Args:
        text_id: Identifiant Légifrance du texte (ex: LEGITEXT000006353440).
        date: Date de consultation YYYY-MM-DD (défaut: aujourd'hui).
    """
    c    = _get_client()
    data = c._req("/consult/lawDecree", body={
        "textId": text_id,
        "date":   _date_today.today().isoformat() if date is None else date,
    })
    titre    = data.get("title", text_id)
    nature   = data.get("nature", "")
    nor      = data.get("nor", "")
    date_pub = data.get("dateParution", "")
    lines    = [f"# {titre}\n"]
    if nature:   lines.append(f"**Nature** : {nature}")
    if nor:      lines.append(f"**NOR** : {nor}")
    if date_pub: lines.append(f"**Publication** : {date_pub}")
    articles = data.get("articles", data.get("sections", []))
    if articles:
        lines.append(f"\n**{len(articles)} article(s)**\n")
        for a in articles[:50]:
            lines.append(_fmt_article(a))
            lines.append("")
        if len(articles) > 50:
            lines.append(f"*… {len(articles)-50} article(s) supplémentaires non affichés*")
    return "\n".join(lines)


@mcp.tool()
def legifrance_legi_part(part_id: str, date: Optional[str] = None) -> str:
    """Récupère une partie d'un texte LEGI par son identifiant de partie.

    Utile pour naviguer dans les sous-sections d'un code ou texte volumineux.

    Args:
        part_id: Identifiant de la partie LEGI.
        date: Date de consultation YYYY-MM-DD (défaut: aujourd'hui).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"id": part_id}
    if date:
        body["date"] = date
    data  = c._req("/consult/legi/getLegiPart", body=body)
    titre = data.get("title", part_id)
    lines = [f"**Partie : {titre}**\n"]
    arts  = data.get("articles", [])
    sects = data.get("sections", [])
    if arts:
        lines.append(f"**{len(arts)} article(s)**\n")
        for a in arts[:20]:
            lines.append(_fmt_article(a))
            lines.append("")
    if sects:
        lines.append(f"*{len(sects)} sous-section(s)*")
    return "\n".join(lines)


@mcp.tool()
def legifrance_tables_annuelles(annee_fin: int, annee_debut: Optional[int] = None) -> str:
    """Récupère la liste des tables annuelles du Journal Officiel disponibles dans Légifrance.

    Les tables annuelles recensent tous les textes publiés au JO pour une période donnée.

    Args:
        annee_fin: Année de fin de la période (ex: 2024).
        annee_debut: Année de début de la période (optionnel).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"endYear": annee_fin}
    if annee_debut is not None:
        body["startYear"] = annee_debut
    data  = c._req("/consult/getTables", body=body)
    items = data.get("tables", [])
    if not items:
        periode = f"{annee_debut}–{annee_fin}" if annee_debut else str(annee_fin)
        return f"Aucune table annuelle trouvée pour la période {periode}."
    lines = [f"**{len(items)} table(s) annuelle(s)**\n"]
    for t in items:
        annee = t.get("annee", t.get("year", ""))
        titre = t.get("titre", t.get("title", ""))
        rid   = t.get("id", "")
        line  = f"- **{annee}**"
        if titre: line += f" — {titre}"
        if rid:   line += f" — `{rid}`"
        lines.append(line)
    return "\n".join(lines)


# ===========================================================================
# OUTILS MCP — Liens d'articles (4)
# ===========================================================================

@mcp.tool()
def legifrance_liens_concordance(article_id: str) -> str:
    """Récupère les liens de concordance d'un article (articles équivalents dans d'autres textes).

    Args:
        article_id: Identifiant de l'article (LEGIARTI...).
    """
    c    = _get_client()
    data = c._req("/consult/concordance", body={"id": article_id})
    items = data.get("liens", data.get("links", []))
    if not items:
        return f"Aucun lien de concordance pour `{article_id}`."
    lines = [f"**{len(items)} lien(s) de concordance**\n"]
    for lien in items[:20]:
        titre = lien.get("titre", lien.get("title", ""))
        rid   = lien.get("id", "")
        lines.append(f"- {titre}" + (f" — `{rid}`" if rid else ""))
    return "\n".join(lines)


@mcp.tool()
def legifrance_liens_relatifs(article_id: str) -> str:
    """Récupère les liens relatifs (citants/cités) d'un article juridique.

    Args:
        article_id: Identifiant de l'article (LEGIARTI...).
    """
    c    = _get_client()
    data = c._req("/consult/relatifs", body={"id": article_id})
    lines = [f"**Liens relatifs de `{article_id}`**\n"]
    for section_name, items in data.items():
        if isinstance(items, list) and items:
            lines.append(f"\n**{section_name}** ({len(items)})")
            for lien in items[:10]:
                t   = lien.get("titre", lien.get("title", ""))
                rid = lien.get("id", "")
                lines.append(f"  - {t}" + (f" — `{rid}`" if rid else ""))
    return "\n".join(lines) if len(lines) > 1 else "Aucun lien relatif trouvé."


@mcp.tool()
def legifrance_liens_service_public(article_id: str) -> str:
    """Récupère les liens vers service-public.fr associés à un article.

    Args:
        article_id: Identifiant de l'article.
    """
    c    = _get_client()
    data = c._req("/consult/getLinksServicePublic", body={"id": article_id})
    items = data.get("liens", data.get("links", []))
    if not items:
        return f"Aucun lien service-public.fr pour `{article_id}`."
    lines = [f"**{len(items)} lien(s) service-public.fr**\n"]
    for lien in items:
        titre = lien.get("titre", lien.get("title", ""))
        url   = lien.get("url", "")
        lines.append(f"- [{titre}]({url})" if url else f"- {titre}")
    return "\n".join(lines)


@mcp.tool()
def legifrance_a_liens_service_public(article_id: str) -> str:
    """Vérifie si un article possède des liens vers service-public.fr.

    Args:
        article_id: Identifiant de l'article.
    """
    c    = _get_client()
    data = c._req("/consult/hasLinksServicePublic", body={"id": article_id})
    has  = data.get("hasLinks", data.get("hasLinksServicePublic", False))
    if has:
        return f"✅ L'article `{article_id}` **possède** des liens service-public.fr. Utilisez `legifrance_liens_service_public` pour les récupérer."
    return f"ℹ️ L'article `{article_id}` **ne possède pas** de liens service-public.fr."


# ===========================================================================
# OUTILS MCP — Consultation JORF (6)
# ===========================================================================

@mcp.tool()
def legifrance_jorf(text_cid: str) -> str:
    """Consulte et retourne le contenu complet d'un texte du Journal Officiel.

    IMPORTANT : text_cid doit etre un Chronical ID (CID) de type JORFTEXT...
    obtenu via legifrance_sommaire_jorf ou legifrance_derniers_jo.
    NE PAS utiliser les IDs de version retournes par legifrance_rechercher.

    Flux recommande :
      legifrance_derniers_jo -> legifrance_sommaire_jorf -> legifrance_jorf

    Args:
        text_cid: Chronical ID du texte JORF (ex: JORFTEXT000053598801).
                  Commence toujours par JORFTEXT, jamais JORFARTI ou autre.
    """
    c      = _get_client()
    data   = c._req("/consult/jorf", body={"textCid": text_cid})
    titre  = data.get("title", text_cid)
    date_p = data.get("dateParution", "")
    nor    = data.get("nor", "")
    nature = data.get("nature", "")
    etat   = data.get("jurisState", data.get("etat", ""))
    eli    = data.get("eli", "")
    lines  = [f"# {titre}\n"]
    if nature:  lines.append(f"**Nature** : {nature}")
    if date_p:  lines.append(f"**Publication JO** : {date_p}")
    if nor:     lines.append(f"**NOR** : {nor}")
    if eli:     lines.append(f"**ELI** : {eli}")
    if etat:    lines.append(f"**Etat** : {etat}")
    # Afficher les articles directement (plus besoin de jorf_part)
    arts  = data.get("articles", [])
    sects = data.get("sections", [])
    all_arts = list(arts)
    for s in sects:
        all_arts.extend(s.get("articles", []))
    if all_arts:
        lines.append(f"\n**{len(all_arts)} article(s)**\n")
        for a in all_arts[:50]:
            lines.append(_fmt_article(a))
            lines.append("")
        if len(all_arts) > 50:
            lines.append(f"*… {len(all_arts)-50} article(s) supplementaires — utilisez legifrance_jorf_part avec les IDs de section*")
    elif sects:
        lines.append(f"\n**{len(sects)} section(s)** — utilisez legifrance_jorf_part avec l'ID de section")
        for s in sects[:10]:
            sid = s.get("id", "")
            t   = s.get("title", s.get("titre", ""))
            lines.append(f"  - {t}" + (f" (`{sid}`)" if sid else ""))
    return "\n".join(lines)


@mcp.tool()
def legifrance_jorf_part(part_id: str) -> str:
    """Récupère une partie d'un texte JORF par son identifiant.

    Args:
        part_id: Identifiant de la partie JORF.
    """
    c    = _get_client()
    data = c._req("/consult/jorfPart", body={"id": part_id})
    titre = data.get("title", part_id)
    lines = [f"**{titre}**\n"]
    arts  = data.get("articles", [])
    for a in arts[:30]:
        lines.append(_fmt_article(a))
        lines.append("")
    if len(arts) > 30:
        lines.append(f"*… {len(arts)-30} article(s) supplémentaires*")
    return "\n".join(lines)


@mcp.tool()
def legifrance_jo_par_nor(nor: str) -> str:
    """Consulte un texte du Journal Officiel par son numéro NOR.

    Le NOR (Numéro d'Ordre du Registre) identifie uniquement chaque texte officiel
    (ex: PRMD2117108D, JUSB2123813A).

    Args:
        nor: Numéro NOR du texte.
    """
    c     = _get_client()
    data  = c._req("/consult/getJoWithNor", body={"nor": nor})
    titre  = data.get("title", nor)
    date_p = data.get("dateParution", "")
    nature = data.get("nature", "")
    etat   = data.get("jurisState", data.get("etat", ""))
    eli    = data.get("eli", "")
    lines  = [f"# {titre}\n", f"**NOR** : {nor}"]
    if nature:  lines.append(f"**Nature** : {nature}")
    if date_p:  lines.append(f"**Publication JO** : {date_p}")
    if eli:     lines.append(f"**ELI** : {eli}")
    if etat:    lines.append(f"**État** : {etat}")
    arts  = data.get("articles", [])
    sects = data.get("sections", [])
    all_arts = list(arts)
    for s in sects:
        all_arts.extend(s.get("articles", []))
    if all_arts:
        lines.append(f"\n**{len(all_arts)} article(s)**\n")
        for a in all_arts[:50]:
            lines.append(_fmt_article(a))
            lines.append("")
        if len(all_arts) > 50:
            lines.append(f"*… {len(all_arts)-50} article(s) supplémentaires — utilisez legifrance_jorf_part avec l\'ID de section*")
    elif sects:
        lines.append(f"\n**{len(sects)} section(s)** — utilisez legifrance_jorf_part avec l\'ID de section")
        for s in sects[:10]:
            sid = s.get("id", "")
            t   = s.get("title", s.get("titre", ""))
            lines.append(f"  - {t}" + (f" (`{sid}`)" if sid else ""))
    return "\n".join(lines)


@mcp.tool()
def legifrance_eli_alias_texte(eli: str) -> str:
    """Résout un alias ELI vers le CID du texte correspondant dans Légifrance.

    Args:
        eli: Identifiant ELI (ex: eli/loi/2016/10/7/2016-1321/jo/texte).
    """
    c    = _get_client()
    data = c._req("/consult/getEliAlias", body={"eli": eli})
    cid  = data.get("cid", data.get("textCid", ""))
    if cid:
        return f"✅ L'ELI `{eli}` correspond au CID : `{cid}`"
    return f"Aucun texte trouvé pour l'ELI : `{eli}`"


@mcp.tool()
def legifrance_derniers_jo(nb: int = 10) -> str:
    """Récupère les dernières parutions du Journal Officiel.

    Retourne la liste des N derniers numéros publiés avec leurs dates et identifiants.

    Args:
        nb: Nombre de JO à récupérer (défaut: 10).
    """
    c    = _get_client()
    data = c._req("/consult/lastNJo", body={"nbElement": nb})
    items = data.get("containers", [])
    if not items:
        return "Aucun Journal Officiel trouvé."
    total = data.get("totalNbResult", len(items))
    lines = [f"**{len(items)} dernier(s) Journal(aux) Officiel(s)** (total API : {total})\n"]
    for jo in items:
        date_p = _fmt_date(jo.get("datePubli", ""))
        num    = jo.get("num", jo.get("numero", ""))
        cid    = jo.get("cid", jo.get("id", ""))
        titre  = jo.get("titre", "")
        label  = f"JO du {date_p}" if date_p else "JO"
        if num:   label += f" n°{num}"
        detail = f" — {titre}" if titre else ""
        lines.append(f"- **{label}**{detail}" + (f" — `{cid}`" if cid else ""))
    return "\n".join(lines)


def _parse_date_to_consult(d: str) -> Dict[str, int]:
    """Convertit YYYY-MM-DD en ConsultDateRequest {year, month, dayOfMonth}."""
    parts = d.split("-")
    req: Dict[str, int] = {"year": int(parts[0])}
    if len(parts) > 1: req["month"] = int(parts[1])
    if len(parts) > 2: req["dayOfMonth"] = int(parts[2])
    return req


@mcp.tool()
def legifrance_sommaire_jorf(
    date: Optional[str] = None,
    date_fin: Optional[str] = None,
    jorf_id: Optional[str] = None,
    recherche: Optional[str] = None,
    nb_resultats: int = 20,
) -> str:
    """Consulte le sommaire du Journal Officiel sur une date ou une période.

    Les CID retournés (ex: JORFTEXT...) peuvent être passés à legifrance_jorf.
    Pour une période (ex: tous les decrets de mars 2026) : date='2026-03-01', date_fin='2026-03-31'.

    Args:
        date: Date de debut YYYY-MM-DD (ex: '2026-03-01').
        date_fin: Date de fin YYYY-MM-DD (ex: '2026-03-31'). Si omis, recherche sur date seule.
        jorf_id: Identifiant conteneur JORF (ex: JORFCONT000049456397). Prioritaire sur date.
        recherche: Texte a rechercher dans les titres du sommaire.
        nb_resultats: Nombre de resultats (defaut: 20, max: 100).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats}
    if jorf_id:
        body["id"] = jorf_id
    elif date:
        # L'API attend ConsultDateRequest {year, month, dayOfMonth}, pas une string ISO
        body["start"] = _parse_date_to_consult(date)
        body["end"]   = _parse_date_to_consult(date_fin if date_fin else date)
    if recherche: body["searchText"] = recherche
    data  = c._req("/consult/jorfCont", body=body)
    items = data.get("items", [])
    label = date or jorf_id or "JO"
    if not items:
        return f"Aucun résultat dans le sommaire du {label}."

    def _collect_structure(structure: Dict, out_lines: List[str], out_textes: List[Dict]) -> None:
        """Parcourt structure.liens et structure.tms récursivement."""
        def collect_liens(liens):
            for l in liens or []:
                out_textes.append(l)

        def collect_tms(tms_list, depth=0):
            for tms in tms_list or []:
                titre_s = tms.get("titre", "")
                if titre_s:
                    out_lines.append(("  " * depth) + f"**{titre_s}**")
                collect_liens(tms.get("liensTxt", []))
                collect_tms(tms.get("tms", []), depth + 1)

        collect_liens(structure.get("liens", []))
        collect_tms(structure.get("tms", []))

    lines: List[str] = []
    for item in items:
        cont = item.get("joCont") or item.get("joInap") or item.get("joEA") or item
        if not cont:
            continue
        titre_jo = cont.get("titre", "Sans titre")
        date_p   = _fmt_date(cont.get("datePubli", ""))
        num      = cont.get("num", "")
        cid_jo   = cont.get("cid", cont.get("id", ""))
        header   = f"## {titre_jo}"
        if date_p or num:
            header += " (" + " | ".join(filter(None, [f"n°{num}" if num else "", date_p])) + ")"
        lines.append(header)
        if cid_jo:
            lines.append(f"Identifiant conteneur : `{cid_jo}`")
        lines.append("")

        # Extraire les textes depuis structure.liens (1er niveau) et structure.tms (sections)
        textes: List[Dict] = []
        _collect_structure(cont.get("structure", {}), lines, textes)

        if textes:
            for t in textes:
                tid      = t.get("id", "")
                ttitre   = t.get("titre", "Sans titre")
                nature   = t.get("nature", "")
                etat     = t.get("etat", "")
                autorite = t.get("autorite", t.get("ministere", ""))
                meta     = " | ".join(filter(None, [nature, etat, autorite]))
                line     = f"- **{ttitre}**"
                if meta: line += f" ({meta})"
                if tid:  line += f" — `{tid}`"
                lines.append(line)
        else:
            lines.append("*(aucun texte dans ce JO ou structure non disponible)*")
        lines.append("")

    return "\n".join(lines)


# ===========================================================================
# OUTILS MCP — Consultation KALI / conventions collectives (5)
# ===========================================================================

@mcp.tool()
def legifrance_convention_par_idcc(idcc: str) -> str:
    """Consulte la structure principale d'une convention collective par son numéro IDCC.

    Args:
        idcc: Numéro IDCC de la convention (ex: '0016', '1486', '3248').
    """
    c     = _get_client()
    data  = c._req("/consult/kaliContIdcc", body={"id": idcc})
    titre  = data.get("titre", f"Convention IDCC {idcc}")
    rid    = data.get("id", "")
    num    = data.get("num", "")
    nature = data.get("nature", "")
    sects  = data.get("sections", [])
    lines  = [f"# {titre}\n", f"**IDCC** : {idcc}"]
    if rid:    lines.append(f"**ID** : `{rid}`")
    if num:    lines.append(f"**Numéro** : {num}")
    if nature: lines.append(f"**Nature** : {nature}")
    if sects:
        lines.append(f"\n**{len(sects)} section(s)**")
        for s in sects[:20]:
            t = s.get("titre", s.get("num", ""))
            if t: lines.append(f"  - {t}")
        if len(sects) > 20:
            lines.append(f"  *… {len(sects)-20} élément(s) supplémentaires*")
    return "\n".join(lines)


@mcp.tool()
def legifrance_convention_cont(cont_id: str) -> str:
    """Consulte le conteneur d'une convention collective par son identifiant KALI.

    Args:
        cont_id: Identifiant du conteneur KALI (KALICONT...).
    """
    c     = _get_client()
    data  = c._req("/consult/kaliCont", body={"id": cont_id})
    titre  = data.get("titre", cont_id)
    idcc   = data.get("idcc", data.get("num", ""))
    sects  = data.get("sections", [])
    lines  = [f"# {titre}\n"]
    if idcc: lines.append(f"**IDCC** : {idcc}")
    if sects:
        lines.append(f"\n**{len(sects)} section(s)**")
        for s in sects[:20]:
            t = s.get("titre", "")
            if t: lines.append(f"  - {t}")
    return "\n".join(lines)


@mcp.tool()
def legifrance_convention_texte(text_id: str) -> str:
    """Consulte le texte complet d'une convention collective par son identifiant KALI.

    Args:
        text_id: Identifiant KALI du texte (ex: KALITEXT000005635374).
    """
    c     = _get_client()
    data  = c._req("/consult/kaliText", body={"id": text_id})
    titre = data.get("title", text_id)
    nor   = data.get("nor", "")
    lines = [f"# {titre}\n"]
    if nor: lines.append(f"**NOR** : {nor}")
    etat = data.get("etat", "")
    if etat: lines.append(f"**État** : {etat}")
    articles = data.get("articles", data.get("sections", []))
    if articles:
        lines.append(f"\n**{len(articles)} article(s)/section(s)**\n")
        for a in articles[:30]:
            lines.append(_fmt_article(a))
            lines.append("")
        if len(articles) > 30:
            lines.append(f"*… {len(articles)-30} élément(s) supplémentaire(s) non affiché(s)*")
    return "\n".join(lines)


@mcp.tool()
def legifrance_convention_article(article_id: str) -> str:
    """Consulte un article spécifique d'une convention collective par son identifiant KALI.

    Args:
        article_id: Identifiant de l'article KALI (ex: KALIARTI000005635375).
    """
    c    = _get_client()
    data = c._req("/consult/kaliArticle", body={"id": article_id})
    return _fmt_article(data)


@mcp.tool()
def legifrance_convention_section(section_id: str) -> str:
    """Consulte une section d'une convention collective par son identifiant KALI.

    Args:
        section_id: Identifiant de la section KALI (ex: KALISECT000005635376).
    """
    c        = _get_client()
    data     = c._req("/consult/kaliSection", body={"id": section_id})
    titre    = data.get("title", section_id)
    articles = data.get("articles", [])
    sects    = data.get("sections", [])
    lines    = [f"**Section : {titre}**\n"]
    if articles:
        lines.append(f"**{len(articles)} article(s)**\n")
        for a in articles:
            lines.append(_fmt_article(a))
            lines.append("")
    if sects:
        lines.append(f"*{len(sects)} sous-section(s)*")
    return "\n".join(lines)


# ===========================================================================
# OUTILS MCP — Consultation jurisprudence (3)
# ===========================================================================

@mcp.tool()
def legifrance_jurisprudence(text_id: str) -> str:
    """Consulte une décision de justice française (jurisprudence).

    Retourne le texte complet de la décision avec juridiction et date.

    Args:
        text_id: Identifiant de la décision (ex: JURITEXT000007587512).
    """
    c     = _get_client()
    data  = c._req("/consult/juri", body={"textId": text_id})
    txt   = data.get("text") or [{}]
    txt   = txt[0] if isinstance(txt, list) else txt
    titre = txt.get("titre", text_id)
    juri  = txt.get("juridiction", "")
    dt    = txt.get("dateTexte", "")
    texte = _strip_html(txt.get("texteHtml", txt.get("texte", "")))
    lines = [f"# {titre}\n"]
    if juri:  lines.append(f"**Juridiction** : {juri}")
    if dt:    lines.append(f"**Date** : {dt}")
    if texte:
        lines.append(f"\n{texte[:5000]}")
        if len(texte) > 5000:
            lines.append("\n*[Texte tronqué]*")
    return "\n".join(lines)


@mcp.tool()
def legifrance_jurisprudence_plan_classement(
    libelle: Optional[str] = None,
    fond: Optional[str] = None,
    niveau: Optional[int] = None,
) -> str:
    """Consulte le plan de classement de la jurisprudence (arborescence thématique).

    Permet de naviguer dans les catégories de jurisprudence par thème ou matière.

    Args:
        libelle: Libellé à rechercher (ex: 'contrat', 'responsabilité').
        fond: Fond juridictionnel (ex: 'juri', 'CASS', 'CE').
        niveau: Niveau hiérarchique (1=racine).
    """
    c    = _get_client()
    body: Dict[str, Any] = {}
    if libelle: body["libelle"] = libelle
    if fond:    body["fond"]    = fond
    if niveau is not None: body["niveau"] = niveau
    data  = c._req("/consult/getJuriPlanClassement", body=body)
    items = data.get("listPlanClassementJuri", [])
    if not items:
        return "Aucune entrée de plan de classement trouvée."
    lines = ["**Plan de classement jurisprudence**\n"]
    for item in items[:30]:
        lib = item.get("libelle", item.get("title", ""))
        rid = item.get("id", "")
        nb  = item.get("nbDocuments", "")
        line = f"- **{lib}**"
        if nb:  line += f" ({nb} décision(s))"
        if rid: line += f" — `{rid}`"
        lines.append(line)
    if len(items) > 30:
        lines.append(f"\n*… {len(items)-30} entrée(s) supplémentaire(s)*")
    return "\n".join(lines)


@mcp.tool()
def legifrance_jurisprudence_ancien_id(ancien_id: str) -> str:
    """Consulte une décision de jurisprudence par son ancien identifiant.

    Args:
        ancien_id: Ancien identifiant de la décision.
    """
    c     = _get_client()
    data  = c._req("/consult/getJuriByOldId", body={"oldId": ancien_id})
    txt   = data.get("text") or [{}]
    txt   = txt[0] if isinstance(txt, list) else txt
    titre = txt.get("titre", ancien_id)
    texte = _strip_html(txt.get("texteHtml", txt.get("texte", "")))
    lines = [f"# {titre}\n"]
    if texte:
        lines.append(f"\n{texte[:5000]}")
    return "\n".join(lines)


# ===========================================================================
# OUTILS MCP — Consultation divers (7)
# ===========================================================================

@mcp.tool()
def legifrance_cnil(text_id: str) -> str:
    """Consulte une délibération ou décision CNIL par son identifiant.

    Args:
        text_id: Identifiant du texte CNIL (ex: CNILTEXT000017651305).
    """
    c     = _get_client()
    data  = c._req("/consult/cnil", body={"textId": text_id})
    txt   = data.get("text") or [{}]
    txt   = txt[0] if isinstance(txt, list) else txt
    titre = txt.get("titre", text_id)
    date_d = txt.get("dateTexte", "")
    texte  = _strip_html(txt.get("texteHtml", txt.get("texte", "")))
    lines  = [f"# {titre}\n"]
    if date_d: lines.append(f"**Date** : {date_d}")
    if texte:
        lines.append(f"\n{texte[:5000]}")
        if len(texte) > 5000:
            lines.append("\n*[Texte tronqué]*")
    return "\n".join(lines)


@mcp.tool()
def legifrance_cnil_ancien_id(ancien_id: str) -> str:
    """Consulte une délibération CNIL par son ancien identifiant.

    Args:
        ancien_id: Ancien identifiant du texte CNIL.
    """
    c     = _get_client()
    data  = c._req("/consult/getCnilByOldId", body={"oldId": ancien_id})
    txt   = data.get("text") or [{}]
    txt   = txt[0] if isinstance(txt, list) else txt
    titre = txt.get("titre", ancien_id)
    texte = _strip_html(txt.get("texteHtml", txt.get("texte", "")))
    lines = [f"# {titre}\n"]
    if texte:
        lines.append(f"\n{texte[:5000]}")
    return "\n".join(lines)


@mcp.tool()
def legifrance_acco(acco_id: str) -> str:
    """Consulte un accord d'entreprise (fonds ACCO) par son identifiant.

    Args:
        acco_id: Identifiant de l'accord d'entreprise.
    """
    c     = _get_client()
    data  = c._req("/consult/acco", body={"id": acco_id})
    acc   = data.get("acco") or [{}]
    acc   = acc[0] if isinstance(acc, list) else acc
    titre = acc.get("titreTexte", acco_id)
    siret = acc.get("siret", "")
    rs    = acc.get("raisonSociale", "")
    date_d = acc.get("dateDepot", "")
    lines  = [f"# {titre}\n"]
    if siret:  lines.append(f"**SIRET** : {siret}")
    if rs:     lines.append(f"**Entreprise** : {rs}")
    if date_d: lines.append(f"**Date de dépôt** : {date_d}")
    return "\n".join(lines)


@mcp.tool()
def legifrance_circulaire(circulaire_id: str) -> str:
    """Consulte une circulaire administrative (fonds CIRC) par son identifiant.

    Args:
        circulaire_id: Identifiant de la circulaire.
    """
    c     = _get_client()
    data  = c._req("/consult/circulaire", body={"id": circulaire_id})
    circ  = data.get("circulaire") or [{}]
    circ  = circ[0] if isinstance(circ, list) else circ
    titre = circ.get("titre", circulaire_id)
    nor   = circ.get("nor", "")
    date_p = circ.get("dateSignature", "")
    lines  = [f"# {titre}\n"]
    if nor:    lines.append(f"**NOR** : {nor}")
    if date_p: lines.append(f"**Date de signature** : {date_p}")
    return "\n".join(lines)


@mcp.tool()
def legifrance_debat(debat_id: str) -> str:
    """Consulte le compte-rendu d'un débat parlementaire par son identifiant.

    Couvre les séances de l'Assemblée Nationale et du Sénat.

    Args:
        debat_id: Identifiant du débat parlementaire.
    """
    c     = _get_client()
    data  = c._req("/consult/debat", body={"id": debat_id})
    deb   = data.get("debat") or [{}]
    deb   = deb[0] if isinstance(deb, list) else deb
    date_s    = deb.get("dateSeance", "")
    assemblee = deb.get("typeAssemblee", "")
    legis     = deb.get("legislature", "")
    session   = deb.get("session", "")
    rid       = deb.get("id", debat_id)
    lines     = [f"# Débat parlementaire `{rid}`\n"]
    if date_s:    lines.append(f"**Séance du** : {date_s}")
    if assemblee: lines.append(f"**Assemblée** : {assemblee}")
    if legis:     lines.append(f"**Législature** : {legis}")
    if session:   lines.append(f"**Session** : {session}")
    return "\n".join(lines)


@mcp.tool()
def legifrance_dossier_legislatif(dossier_id: str) -> str:
    """Consulte un dossier législatif complet par son identifiant.

    Regroupe tous les documents liés à l'élaboration d'une loi :
    projets, amendements, rapports, textes adoptés.

    Args:
        dossier_id: Identifiant du dossier législatif.
    """
    c     = _get_client()
    data  = c._req("/consult/dossierLegislatif", body={"id": dossier_id})
    dos   = data.get("dossierLegislatif") or [{}]
    dos   = dos[0] if isinstance(dos, list) else dos
    titre = dos.get("titre", dossier_id)
    type_d = dos.get("type", "")
    legis  = dos.get("legislature", "")
    date_c = dos.get("dateCreation", "")
    date_m = dos.get("dateDerniereModification", "")
    lines  = [f"# {titre}\n"]
    if type_d: lines.append(f"**Type** : {type_d}")
    if legis:  lines.append(f"**Législature** : {legis}")
    if date_c: lines.append(f"**Créé le** : {date_c}")
    if date_m: lines.append(f"**Modifié le** : {date_m}")
    return "\n".join(lines)


@mcp.tool()
def legifrance_section_par_cid(cid: str) -> str:
    """Récupère le contenu d'une section de code ou de texte par son CID canonique.

    Utile pour naviguer dans la structure d'un code (format LEGISCTA...).

    Args:
        cid: CID de la section (ex: LEGISCTA000006150321).
    """
    c     = _get_client()
    data  = c._req("/consult/getSectionByCid", body={"cid": cid})
    sects = data.get("listSection", [])
    if not sects:
        return f"Aucune section trouvée pour le CID `{cid}`."
    lines = [f"**{len(sects)} section(s) pour** `{cid}`\n"]
    for s in sects[:20]:
        titre_s = s.get("titre", "")
        cid_s   = s.get("cid", s.get("id", ""))
        debut   = s.get("dateDebut", "")
        lines.append(f"- **{titre_s}**" + (f" `{cid_s}`" if cid_s else "") + (f" ({debut})" if debut else ""))
    return "\n".join(lines)


# ===========================================================================
# OUTILS MCP — Consultation BOCC (1)
# ===========================================================================

@mcp.tool()
def legifrance_bocc_pdf_metadata(
    bocc_id: Optional[str] = None,
    pour_bocc_global: Optional[bool] = None,
) -> str:
    """Récupère les métadonnées du PDF associé à un texte unitaire du BOCC.

    Le BOCC est le Bulletin Officiel des Conventions Collectives.

    Args:
        bocc_id: Identifiant du texte BOCC (optionnel).
        pour_bocc_global: True pour cibler le BOCC global plutôt qu'un texte unitaire.
    """
    c    = _get_client()
    body: Dict[str, Any] = {}
    if bocc_id: body["id"] = bocc_id
    if pour_bocc_global is not None: body["forGlobalBocc"] = pour_bocc_global
    data = c._req("/consult/getBoccTextPdfMetadata", body=body)
    if not data:
        return "Aucune métadonnée PDF BOCC trouvée."
    lines = ["**Métadonnées PDF BOCC**\n"]
    for k, v in data.items():
        if v: lines.append(f"- **{k}** : {v}")
    return "\n".join(lines) if len(lines) > 1 else "Aucune métadonnée disponible."


# ===========================================================================
# OUTILS MCP — Chrono (3)
# ===========================================================================

@mcp.tool()
def legifrance_historique_texte(
    text_cid: str,
    annee_debut: int,
    annee_fin: int,
    date_consult: Optional[str] = None,
) -> str:
    """Récupère l'historique complet des versions d'un texte juridique sur une période.

    Permet de suivre l'évolution législative d'un code ou d'une loi dans le temps.

    Args:
        text_cid: CID chronologique du texte (ex: LEGITEXT000006070721 pour le Code civil).
        annee_debut: Année de début de la période (ex: 2015).
        annee_fin: Année de fin de la période (ex: 2024).
        date_consult: Date de référence YYYY-MM-DD (défaut: aujourd'hui).
    """
    c = _get_client()
    if date_consult is None:
        date_consult = _date_today.today().isoformat()
    data = c._req("/chrono/textCid", body={
        "textCid":     text_cid,
        "dateConsult": date_consult,
        "startYear":   annee_debut,
        "endYear":     annee_fin,
    })
    regroupements = data.get("regroupements", [])
    if not regroupements:
        return f"Aucune version trouvée pour `{text_cid}` entre {annee_debut} et {annee_fin}."
    lines = [f"**{len(regroupements)} regroupement(s) de `{text_cid}` ({annee_debut}–{annee_fin})**\n"]
    for rg in regroupements:
        title_rg = rg.get("title", "")
        vers     = rg.get("versions", [])
        lines.append(f"- **{title_rg}** ({len(vers)} version(s))")
    return "\n".join(lines)


@mcp.tool()
def legifrance_versions_element(text_cid: str, element_cid: str) -> str:
    """Récupère toutes les versions chronologiques d'un article ou d'une section.

    Permet de voir toutes les modifications d'un article au sein d'un texte donné.

    Args:
        text_cid: CID du texte contenant l'élément (ex: LEGITEXT000006070721).
        element_cid: CID de l'article ou de la section.
    """
    c    = _get_client()
    data = c._req("/chrono/textCidAndElementCid", body={
        "textCid":    text_cid,
        "elementCid": element_cid,
    })
    regroupements = data.get("regroupements", [])
    if not regroupements:
        return f"Aucune version trouvée pour l'élément `{element_cid}`."
    lines = [f"**{len(regroupements)} regroupement(s) pour `{element_cid}`**\n"]
    for rg in regroupements:
        title_rg = rg.get("title", "")
        vers     = rg.get("versions", [])
        lines.append(f"- **{title_rg}** ({len(vers)} version(s))")
    return "\n".join(lines)


@mcp.tool()
def legifrance_a_des_versions(text_cid: str) -> str:
    """Vérifie rapidement si un texte juridique possède des versions historiques.

    Utile avant d'appeler legifrance_historique_texte.

    Args:
        text_cid: CID du texte (ex: LEGITEXT000006070721).
    """
    c    = _get_client()
    data = c._req(f"/chrono/textCid/{text_cid}", method="GET")
    has  = data.get("hasChronolegi", data.get("hasVersions", False))
    if has:
        return f"✅ Le texte `{text_cid}` **possède** un historique de versions. Utilisez `legifrance_historique_texte` pour les récupérer."
    return f"ℹ️ Le texte `{text_cid}` **ne possède pas** de versions historiques enregistrées."


# ===========================================================================
# OUTILS MCP — List (12)
# ===========================================================================

@mcp.tool()
def legifrance_lister_codes(filtre: Optional[str] = None, en_vigueur_seulement: bool = True) -> str:
    """Liste tous les codes juridiques disponibles dans Légifrance avec leurs identifiants.

    Args:
        filtre: Filtre par nom (ex: 'code de la route', 'environnement').
        en_vigueur_seulement: Retourner uniquement les codes en vigueur (défaut: true).
    """
    c     = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": 100}
    if filtre:               body["codeName"] = filtre
    if en_vigueur_seulement: body["states"]   = ["VIGUEUR"]
    data  = c._req("/list/code", body=body)
    codes = data.get("results", [])
    if not codes:
        return "Aucun code trouvé."
    lines = [f"**{len(codes)} code(s)**\n"]
    for code in codes:
        t   = code.get("titre", code.get("title", ""))
        rid = code.get("id",    code.get("cid", ""))
        lines.append(f"- **{t}**" + (f" — `{rid}`" if rid else ""))
    return "\n".join(lines)


@mcp.tool()
def legifrance_conventions(
    titre: Optional[str] = None,
    idcc: Optional[str] = None,
    nb_resultats: int = 20,
) -> str:
    """Liste et recherche les conventions collectives disponibles dans Légifrance (fonds KALI).

    Args:
        titre: Recherche par titre (ex: 'métallurgie', 'bâtiment').
        idcc: Numéro IDCC exact (ex: '0016', '1486').
        nb_resultats: Nombre de résultats (défaut: 20).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats}
    if titre: body["titre"] = titre
    if idcc:  body["idcc"]  = idcc
    data  = c._req("/list/conventions", body=body)
    items = data.get("results", [])
    if not items:
        return "Aucune convention collective trouvée."
    lines = [f"**{len(items)} convention(s)**\n"]
    for cv in items:
        t    = cv.get("titre", cv.get("title", ""))
        idcc_val = cv.get("idcc", "")
        rid  = cv.get("id", "")
        lines.append(
            f"- **{t}**"
            + (f" (IDCC {idcc_val})" if idcc_val else "")
            + (f" — `{rid}`"         if rid      else "")
        )
    return "\n".join(lines)


@mcp.tool()
def legifrance_lister_loda(
    natures: Optional[List[str]] = None,
    en_vigueur_seulement: bool = False,
    date_debut: Optional[str] = None,
    date_fin: Optional[str] = None,
    nb_resultats: int = 20,
) -> str:
    """Liste les lois, ordonnances et décrets autonomes disponibles dans Légifrance (fonds LODA).

    Args:
        natures: Natures à filtrer (ex: ['LOI', 'ORDONNANCE', 'DECRET', 'ARRETE']).
        en_vigueur_seulement: Retourner uniquement les textes en vigueur (défaut: false).
        date_debut: Date de publication minimale YYYY-MM-DD.
        date_fin: Date de publication maximale YYYY-MM-DD.
        nb_resultats: Nombre de résultats (défaut: 20).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats}
    if natures:               body["natures"]     = natures
    if en_vigueur_seulement:  body["legalStatus"] = ["VIGUEUR"]
    pub_date: Dict[str, str] = {}
    if date_debut: pub_date["start"] = date_debut
    if date_fin:   pub_date["end"]   = date_fin
    if pub_date:   body["publicationDate"] = pub_date
    data  = c._req("/list/loda", body=body)
    items = data.get("results", [])
    if not items:
        return "Aucun texte LODA trouvé."
    total = data.get("totalResultNumber", len(items))
    lines = [f"**{total} texte(s) LODA** — {len(items)} affiché(s)\n"]
    for t in items:
        titre = t.get("titre", "")
        etat  = t.get("etat", "")
        cid   = t.get("cid", t.get("id", ""))
        debut = t.get("dateDebut", "")
        line  = f"- **{titre}**"
        if etat:  line += f" ({etat})"
        if debut: line += f" — {debut}"
        if cid:   line += f" — `{cid}`"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def legifrance_lister_legislatures() -> str:
    """Liste toutes les législatures disponibles dans Légifrance.

    Retourne les identifiants nécessaires pour lister les dossiers législatifs.
    """
    c     = _get_client()
    data  = c._req("/list/legislatures", body={})
    items = data.get("legislatures", [])
    if not items:
        return "Aucune législature trouvée."
    lines = [f"**{len(items)} législature(s)**\n"]
    for leg in items:
        num   = leg.get("numero", leg.get("num", leg.get("id", "")))
        debut = leg.get("dateDebut", leg.get("startDate", ""))
        fin   = leg.get("dateFin",   leg.get("endDate", "en cours"))
        rid   = leg.get("id", "")
        line  = f"- **Législature {num}**"
        if debut: line += f" : {debut} → {fin}"
        if rid:   line += f" — `{rid}`"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def legifrance_lister_dossiers_legislatifs(
    legislature_id: int,
    type_dossier: str = "LOI_PUBLIEE",
) -> str:
    """Liste les dossiers législatifs d'une législature donnée.

    Utilisez legifrance_lister_legislatures pour obtenir l'ID de législature.

    Args:
        legislature_id: Numéro de la législature (ex: 16 pour la XVIe législature).
        type_dossier: Type de dossier (ex: 'LOI_PUBLIEE', 'EN_COURS', 'LOI_ORDINAIRE').
    """
    c    = _get_client()
    data = c._req("/list/dossiersLegislatifs", body={
        "legislatureId": legislature_id,
        "type":          type_dossier,
    })
    items = data.get("dossiersLegislatifs", [])
    if not items:
        return f"Aucun dossier législatif trouvé pour la législature {legislature_id} (type: {type_dossier})."
    lines = [f"**{len(items)} dossier(s)** — législature {legislature_id} / {type_dossier}\n"]
    for d in items:
        titre  = d.get("titre", "Sans titre")
        rid    = d.get("id", "")
        type_d = d.get("type", "")
        line   = f"- **{titre}**"
        if type_d: line += f" ({type_d})"
        if rid:    line += f" — `{rid}`"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def legifrance_lister_debats_parlementaires(
    types_publication: Optional[List[str]] = None,
    date_parution: Optional[str] = None,
    nb_resultats: int = 20,
) -> str:
    """Liste les débats parlementaires disponibles dans Légifrance.

    Args:
        types_publication: Types de publication (['AN'] pour Assemblée Nationale, ['SENAT']).
        date_parution: Date de parution YYYY-MM-DD.
        nb_resultats: Nombre de résultats (défaut: 20).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats}
    if types_publication: body["typesPublication"] = types_publication
    if date_parution:     body["dateParution"]     = date_parution
    data  = c._req("/list/debatsParlementaires", body=body)
    items = data.get("results", [])
    if not items:
        return "Aucun débat parlementaire trouvé."
    total = data.get("totalResultNumber", len(items))
    lines = [f"**{total} débat(s)** — {len(items)} affiché(s)\n"]
    for d in items:
        date_s    = d.get("dateSeance", d.get("dateParution", ""))
        assemblee = d.get("typeAssemblee", "")
        rid       = d.get("id", "")
        line      = f"- **{date_s or rid}**"
        if assemblee: line += f" ({assemblee})"
        if rid:       line += f" — `{rid}`"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def legifrance_lister_questions_parlementaires(
    parlement_types: Optional[List[str]] = None,
    periode_publication: Optional[str] = None,
    nb_resultats: int = 20,
) -> str:
    """Liste les questions écrites parlementaires disponibles dans Légifrance.

    Args:
        parlement_types: Types de parlement (['AN'] pour Assemblée Nationale, ['SENAT']).
        periode_publication: Période de publication (ex: '01/01/2024 > 31/12/2024').
        nb_resultats: Nombre de résultats (défaut: 20).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats}
    if parlement_types:     body["parlement"]          = parlement_types
    if periode_publication: body["periodePublication"] = periode_publication
    data  = c._req("/list/questionsParlementaires", body=body)
    items = data.get("results", [])
    if not items:
        return "Aucune question parlementaire trouvée."
    total = data.get("totalResultNumber", len(items))
    lines = [f"**{total} question(s)** — {len(items)} affichée(s)\n"]
    for q in items:
        titre   = q.get("titre", q.get("title", "Sans titre"))
        parl    = q.get("typeParlement", "")
        date_p  = q.get("datePublication", "")
        rid     = q.get("id", "")
        line    = f"- **{titre}**"
        if parl:   line += f" ({parl})"
        if date_p: line += f" — {date_p}"
        if rid:    line += f" — `{rid}`"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def legifrance_lister_bocc(
    interval_publication: Optional[str] = None,
    nb_resultats: int = 20,
) -> str:
    """Liste les numéros du BOCC (Bulletin Officiel des Conventions Collectives).

    Args:
        interval_publication: Intervalle de publication (ex: '01/01/2024 > 31/12/2024').
        nb_resultats: Nombre de résultats (défaut: 20).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats}
    if interval_publication: body["intervalPublication"] = interval_publication
    data  = c._req("/list/bocc", body=body)
    items = data.get("results", [])
    if not items:
        return "Aucun BOCC trouvé."
    total = data.get("totalResultNumber", len(items))
    lines = [f"**{total} BOCC** — {len(items)} affiché(s)\n"]
    for b in items:
        rid  = b.get("id", "")
        glob = b.get("globalBocc") or {}
        num  = glob.get("numero", glob.get("num", ""))
        line = f"- **BOCC {num or rid}**"
        if rid: line += f" — `{rid}`"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def legifrance_lister_bocc_textes(
    bocc_id: Optional[str] = None,
    idccs: Optional[List[str]] = None,
    nb_resultats: int = 20,
) -> str:
    """Liste les textes d'un numéro de BOCC donné.

    Args:
        bocc_id: Identifiant du BOCC principal.
        idccs: Liste d'IDCC pour filtrer (ex: ['0016', '1486']).
        nb_resultats: Nombre de résultats (défaut: 20).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats}
    if bocc_id: body["idMainBocc"] = bocc_id
    if idccs:   body["idccs"]      = idccs
    data  = c._req("/list/boccTexts", body=body)
    items = data.get("texts", [])
    if not items:
        return "Aucun texte BOCC trouvé."
    total = data.get("totalResultNumber", len(items))
    lines = [f"**{total} texte(s) BOCC** — {len(items)} affiché(s)\n"]
    for t in items:
        titre    = t.get("title", t.get("enteteTitle", "Sans titre"))
        idccs_t  = t.get("idccs", [])
        idcc_str = ", ".join(str(i) for i in idccs_t[:3]) if idccs_t else ""
        line     = f"- **{titre}**"
        if idcc_str: line += f" (IDCC {idcc_str})"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def legifrance_lister_boccs_et_textes(
    idcc: Optional[str] = None,
    titre: Optional[str] = None,
    interval_publication: Optional[str] = None,
    nb_resultats: int = 20,
) -> str:
    """Recherche combinée dans les BOCCs et leurs textes associés.

    Args:
        idcc: Numéro IDCC (ex: '0016').
        titre: Texte à rechercher dans les titres.
        interval_publication: Intervalle de publication (ex: '01/01/2024 > 31/12/2024').
        nb_resultats: Nombre de résultats (défaut: 20).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats}
    if idcc:                 body["idcc"]                = idcc
    if titre:                body["titre"]               = titre
    if interval_publication: body["intervalPublication"] = interval_publication
    data  = c._req("/list/boccsAndTexts", body=body)
    items = data.get("results", [])
    if not items:
        return "Aucun résultat BOCC/texte trouvé."
    total = data.get("totalResultNumber", len(items))
    lines = [f"**{total} résultat(s)** — {len(items)} affiché(s)\n"]
    for t in items:
        titre_item = t.get("titre", t.get("title", "Sans titre"))
        idcc_item  = t.get("idcc", "")
        date_p     = t.get("datePublication", t.get("date", ""))
        rid        = t.get("id", "")
        line       = f"- **{titre_item}**"
        if idcc_item: line += f" (IDCC {idcc_item})"
        if date_p:    line += f" — {date_p}"
        if rid:       line += f" — `{rid}`"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def legifrance_lister_docs_admins(annees: Optional[List[int]] = None) -> str:
    """Liste les documents administratifs disponibles dans Légifrance pour une ou plusieurs années.

    Args:
        annees: Liste d'années à rechercher (ex: [2023, 2024]).
    """
    c    = _get_client()
    body: Dict[str, Any] = {}
    if annees: body["years"] = annees
    data  = c._req("/list/docsAdmins", body=body)
    items = data.get("results", [])
    if not items:
        return "Aucun document administratif trouvé."
    lines = [f"**{len(items)} document(s) administratif(s)**\n"]
    for d in items:
        titre = d.get("titre", d.get("title", "Sans titre"))
        annee = d.get("annee", d.get("year", ""))
        rid   = d.get("id", "")
        line  = f"- **{titre}**"
        if annee: line += f" ({annee})"
        if rid:   line += f" — `{rid}`"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def legifrance_lister_bodmr(
    annee: Optional[int] = None,
    nb_resultats: int = 20,
) -> str:
    """Liste les Bulletins Officiels du Ministère des Relations avec le Parlement (BODMR).

    Args:
        annee: Année à filtrer (ex: 2024).
        nb_resultats: Nombre de résultats (défaut: 20).
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats}
    if annee: body["year"] = annee
    data  = c._req("/list/bodmr", body=body)
    items = data.get("results", [])
    if not items:
        return "Aucun BODMR trouvé."
    total = data.get("totalResultNumber", len(items))
    lines = [f"**{total} BODMR** — {len(items)} affiché(s)\n"]
    for b in items:
        titre = b.get("titre", b.get("title", "Sans titre"))
        annee_b = b.get("annee", b.get("year", ""))
        rid   = b.get("id", "")
        line  = f"- **{titre}**"
        if annee_b: line += f" ({annee_b})"
        if rid:     line += f" — `{rid}`"
        lines.append(line)
    return "\n".join(lines)


# ===========================================================================
# OUTILS MCP — Suggest (3)
# ===========================================================================

@mcp.tool()
def legifrance_suggerer(texte: str) -> str:
    """Suggestions et autocomplétion pour identifier un texte juridique.

    Utile avant une recherche ou consultation quand on connaît le début du nom.

    Args:
        texte: Début du nom à compléter (ex: 'code de la consom', 'loi du 17 juil').
    """
    c    = _get_client()
    data = c._req("/suggest", body={"searchText": texte})
    results = data.get("results", {})
    if not results:
        return f"Aucune suggestion pour : « {texte} »"
    total = data.get("totalResultNumber", "")
    lines = [f"**Suggestions pour « {texte} »**" + (f" ({total})" if total else "") + "\n"]
    if isinstance(results, dict):
        for fond, items in list(results.items())[:5]:
            for s in (items if isinstance(items, list) else [])[:5]:
                t   = s.get("title", s.get("titre", s.get("text", str(s))))
                rid = s.get("id", "")
                lines.append(f"- [{fond}] {t}" + (f" (`{rid}`)" if rid else ""))
    return "\n".join(lines)


@mcp.tool()
def legifrance_suggerer_acco(texte: str) -> str:
    """Suggestions de SIRET et raisons sociales pour les accords d'entreprise.

    Args:
        texte: Début de la raison sociale ou numéro SIRET (ex: 'Renault', 'Airbus', '351').
    """
    c    = _get_client()
    data = c._req("/suggest/acco", body={"searchText": texte})
    results = data.get("results", {})
    if not results:
        return f"Aucune suggestion entreprise pour : « {texte} »"
    lines = [f"**Entreprises suggérées pour « {texte} »**\n"]
    if isinstance(results, dict):
        for k, items in list(results.items())[:3]:
            for s in (items if isinstance(items, list) else [])[:5]:
                rs   = s.get("raisonSociale", s.get("rs", s.get("title", str(s))))
                siret = s.get("siret", "")
                lines.append(f"- **{rs}**" + (f" (SIRET: {siret})" if siret else ""))
    return "\n".join(lines)


@mcp.tool()
def legifrance_suggerer_pdc(texte: str) -> str:
    """Suggestions pour le plan de classement des débats parlementaires.

    Args:
        texte: Début du terme à compléter.
    """
    c    = _get_client()
    data = c._req("/suggest/pdc", body={"searchText": texte})
    results = data.get("results", {})
    if not results:
        return f"Aucune suggestion PDC pour : « {texte} »"
    lines = [f"**Suggestions PDC pour « {texte} »**\n"]
    if isinstance(results, dict):
        for k, items in list(results.items())[:5]:
            for s in (items if isinstance(items, list) else [])[:5]:
                t   = s.get("title", s.get("titre", str(s)))
                rid = s.get("id", "")
                lines.append(f"- {t}" + (f" (`{rid}`)" if rid else ""))
    return "\n".join(lines)


# ===========================================================================
# OUTILS MCP — Misc (3)
# ===========================================================================

@mcp.tool()
def legifrance_dates_sans_jo(annee: int) -> str:
    """Retourne les dates sans Journal Officiel pour une année donnée.

    Utile pour connaître les jours sans publication du JO (week-ends, jours fériés...).

    Args:
        annee: Année concernée (ex: 2024).
    """
    c    = _get_client()
    data = c._req("/misc/datesWithoutJo", body={"year": annee})
    dates = data.get("dates", data.get("datesWithoutJo", []))
    if not dates:
        return f"Aucune date sans JO trouvée pour {annee}."
    lines = [f"**{len(dates)} date(s) sans Journal Officiel en {annee}**\n"]
    for d in dates:
        lines.append(f"- {d}")
    return "\n".join(lines)


@mcp.tool()
def legifrance_annees_sans_table() -> str:
    """Retourne les années sans table annuelle du Journal Officiel disponible dans Légifrance.

    Utile avant d'appeler legifrance_tables_annuelles.
    """
    c    = _get_client()
    data = c._req("/misc/yearsWithoutTable", body={})
    annees = data.get("years", data.get("annees", []))
    if not annees:
        return "Toutes les années ont une table annuelle disponible."
    return f"**Années sans table annuelle** : {', '.join(str(a) for a in annees)}"


@mcp.tool()
def legifrance_commit_id() -> str:
    """Retourne l'identifiant du commit de déploiement courant de l'API Légifrance.

    Utile pour identifier la version de l'API en production.
    """
    c    = _get_client()
    data = c._req("/misc/commitId", method="GET")
    cid  = data.get("commitId", data.get("commit_id", data.get("content", str(data))))
    return f"**Commit ID de l'API Légifrance** : `{cid}`"


# ===========================================================================
# Point d'entrée
# ===========================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Serveur MCP HTTP Streamable — API Légifrance")
    parser.add_argument("--host", default="0.0.0.0", help="Adresse d'écoute (défaut: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=6502, help="Port d'écoute (défaut: 6502)")
    parser.add_argument("--path", default="/mcp", help="Chemin MCP (défaut: /mcp)")
    args = parser.parse_args()

    # FastMCP 1.x : host/port/path se configurent via mcp.settings,
    # run() n'accepte que (transport, mount_path).
    mcp.settings.host                 = args.host
    mcp.settings.port                 = args.port
    mcp.settings.streamable_http_path = args.path

    logger.info(f"🚀 Démarrage du serveur MCP Légifrance sur http://{args.host}:{args.port}{args.path}")
    logger.info("   62 outils disponibles — spec MCP 2025-03-26 HTTP Streamable")
    if not os.getenv("LEGIFRANCE_CLIENT_ID") or not os.getenv("LEGIFRANCE_CLIENT_SECRET"):
        logger.warning("   ⚠️  LEGIFRANCE_CLIENT_ID / LEGIFRANCE_CLIENT_SECRET non définis")
        logger.warning("   ⚠️  Le serveur démarre mais les outils retourneront une erreur sans ces variables")
        logger.warning("   ⚠️  Obtenez vos credentials sur https://piste.gouv.fr")
    else:
        logger.info("   ✅ Credentials Légifrance détectés")

    mcp.settings.stateless_http = True

    mcp.run(transport="streamable-http")
