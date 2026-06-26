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
# Décorateur de sécurité — capture toutes les exceptions dans les outils MCP
# ---------------------------------------------------------------------------
# POURQUOI : FastMCP propage les exceptions Python comme des erreurs de protocole
# MCP (pas comme des tool_result). Lorsque Demeter reçoit une erreur de protocole
# au lieu d'un tool_result, il inscrit un assistant turn vide dans l'historique
# de conversation. Mistral rejette alors le message suivant avec :
#   400 "Assistant message must have either content or tool_calls, but not none."
# La conversation entière est corrompue pour le reste de la session.
#
# La solution : chaque outil retourne TOUJOURS une chaîne, même en cas d'erreur.
import functools as _functools

_original_tool = mcp.tool

def _safe_tool_decorator(*args, **kwargs):
    """Wrapper de mcp.tool() qui enveloppe chaque outil dans un try/except global."""
    original_decorator = _original_tool(*args, **kwargs)
    def decorator(fn):
        @_functools.wraps(fn)
        def wrapper(*fargs, **fkwargs):
            # ── Log de l'appel outil ────────────────────────────────────────
            arg_summary = ", ".join(
                [repr(a)[:80] for a in fargs]
                + [f"{k}={repr(v)[:80]}" for k, v in fkwargs.items()]
            )
            logger.info(f"[OUTIL] {fn.__name__}({arg_summary})")
            try:
                result = fn(*fargs, **fkwargs)
                if result is None:
                    empty_msg = f"(L'outil {fn.__name__} n'a retourné aucun résultat)"
                    logger.warning(f"[OUTIL] {fn.__name__} → résultat vide")
                    return empty_msg
                # Log résumé du résultat (sans le texte complet)
                result_preview = str(result)[:120].replace("\n", " ")
                logger.info(f"[OUTIL] {fn.__name__} → {len(str(result))} car. : {result_preview}…")
                return result
            except Exception as exc:
                import traceback
                tb = traceback.format_exc().split("\n")[-3:-1]
                short = " | ".join(l.strip() for l in tb if l.strip())
                logger.error(f"[OUTIL] {fn.__name__} ERREUR : {exc} ({short})")
                return (
                    f"⚠️ Erreur dans {fn.__name__} : {exc}\n"
                    f"Détail : {short}\n"
                    f"Reformulez la requête ou essayez des paramètres différents."
                )
        return original_decorator(wrapper)
    return decorator

mcp.tool = _safe_tool_decorator

# ---------------------------------------------------------------------------
# Client Légifrance (OAuth2 PISTE, singleton)
# ---------------------------------------------------------------------------

class _LegifranceClient:
    """Client OAuth2 PISTE — singleton, partagé par tous les outils MCP."""

    PRODUCTION_BASE_URL = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
    OAUTH_URL           = "https://oauth.piste.gouv.fr/api/oauth/token"

    # -----------------------------------------------------------------------
    # Dictionnaire COMPLET des codes Légifrance — source officielle DILA.
    # 79 codes + aliases LLM. 4 IDs corrigés vs version précédente :
    #   penitentiaire : 000043395876 → 000045476241
    #   aviation_civile : 000006072301 → 000006074234
    #   rural/rural_peche_maritime : 000006071367 → 000022197698
    #   deontologie (alias dangereux) → supprimé, remplacé par deontologie_architectes
    #   propriete_personnes_publiques / g3p = 000006070299 (pas deontologie)
    # -----------------------------------------------------------------------
    CODE_IDS: Dict[str, str] = {
        # ── Codes classiques ────────────────────────────────────────────────
        "civil":                          "LEGITEXT000006070721",
        "penal":                          "LEGITEXT000006070719",
        "commerce":                       "LEGITEXT000005634379",
        "travail":                        "LEGITEXT000006072050",
        "consommation":                   "LEGITEXT000006069565",
        "procedure_civile":               "LEGITEXT000006070716",
        "procedure_penale":               "LEGITEXT000006071154",
        "sante_publique":                 "LEGITEXT000006072665",
        "education":                      "LEGITEXT000006071191",
        "environnement":                  "LEGITEXT000006074220",
        "securite_sociale":               "LEGITEXT000006073189",
        "urbanisme":                      "LEGITEXT000006074075",
        "propriete_intellectuelle":       "LEGITEXT000006069414",
        "assurances":                     "LEGITEXT000006073984",
        "monetaire_financier":            "LEGITEXT000006072026",
        "patrimoine":                     "LEGITEXT000006074236",
        # ── Fonction publique / administration ──────────────────────────────
        "general_fonction_publique":      "LEGITEXT000044416551",
        "fonction_publique":              "LEGITEXT000044416551",
        "cgfp":                           "LEGITEXT000044416551",
        "general_collectivites":          "LEGITEXT000006070633",
        "collectivites_territoriales":    "LEGITEXT000006070633",
        "cgct":                           "LEGITEXT000006070633",
        "relations_public_administration":"LEGITEXT000031366350",
        "crpa":                           "LEGITEXT000031366350",
        "commande_publique":              "LEGITEXT000037701019",
        "propriete_personnes_publiques":  "LEGITEXT000006070299",
        "g3p":                            "LEGITEXT000006070299",
        "service_national":               "LEGITEXT000006071335",
        # ── Justice / procédure ─────────────────────────────────────────────
        "organisation_judiciaire":        "LEGITEXT000006071164",
        "justice_administrative":         "LEGITEXT000006070933",
        "cja":                            "LEGITEXT000006070933",
        "justice_militaire":              "LEGITEXT000006071360",
        "justice_penale_mineurs":         "LEGITEXT000039086952",
        "penitentiaire":                  "LEGITEXT000045476241",
        "procedures_civiles_execution":   "LEGITEXT000025024948",
        "juridictions_financieres":       "LEGITEXT000006070249",
        # ── Fiscal / financier ──────────────────────────────────────────────
        "general_impots":                 "LEGITEXT000006069577",
        "impots":                         "LEGITEXT000006069577",
        "cgi":                            "LEGITEXT000006069577",
        "cgi_annexe_1":                   "LEGITEXT000006069568",
        "cgi_annexe_2":                   "LEGITEXT000006069569",
        "cgi_annexe_3":                   "LEGITEXT000006069574",
        "cgi_annexe_4":                   "LEGITEXT000006069576",
        "procedures_fiscales":            "LEGITEXT000006069583",
        "lpf":                            "LEGITEXT000006069583",
        "douanes":                        "LEGITEXT000006071570",
        "impositions_biens_services":     "LEGITEXT000044595989",
        # ── Social / santé / famille ────────────────────────────────────────
        "action_sociale_familles":        "LEGITEXT000006074069",
        "famille_aide_sociale":           "LEGITEXT000006072637",
        "mutualite":                      "LEGITEXT000006074067",
        "pensions_civiles_militaires":    "LEGITEXT000006070302",
        "pensions_invalides_guerre":      "LEGITEXT000031712069",
        "pensions_marins":                "LEGITEXT000006074066",
        # ── Construction / urbanisme ─────────────────────────────────────────
        "construction_habitation":        "LEGITEXT000006074096",
        "habitation":                     "LEGITEXT000006074096",
        "expropriation":                  "LEGITEXT000006074224",
        "voirie_routiere":                "LEGITEXT000006070667",
        "domaine_fluvial":                "LEGITEXT000006074237",
        "domaine_etat":                   "LEGITEXT000006070208",
        # ── Environnement / énergie / rural ─────────────────────────────────
        "energie":                        "LEGITEXT000023983208",
        "rural_peche_maritime":           "LEGITEXT000022197698",
        "rural":                          "LEGITEXT000022197698",
        "rural_ancien":                   "LEGITEXT000006071366",
        "forestier":                      "LEGITEXT000025244092",
        "minier":                         "LEGITEXT000023501962",
        "minier_ancien":                  "LEGITEXT000006071785",
        "ports_maritimes":                "LEGITEXT000006074233",
        "route":                          "LEGITEXT000006074228",
        # ── Transports / communications ──────────────────────────────────────
        "transports":                     "LEGITEXT000023086525",
        "aviation_civile":                "LEGITEXT000006074234",
        "postes_communications":          "LEGITEXT000006070987",
        "travail_maritime":               "LEGITEXT000006072051",
        # ── Défense / sécurité ───────────────────────────────────────────────
        "defense":                        "LEGITEXT000006071307",
        "securite_interieure":            "LEGITEXT000025503132",
        "discipline_marine_marchande":    "LEGITEXT000006071188",
        # ── Divers ──────────────────────────────────────────────────────────
        "electoral":                      "LEGITEXT000006070239",
        "elections":                      "LEGITEXT000006070239",
        "recherche":                      "LEGITEXT000006071190",
        "sport":                          "LEGITEXT000006071318",
        "tourisme":                       "LEGITEXT000006074073",
        "cinema":                         "LEGITEXT000020908868",
        "artisanat":                      "LEGITEXT000006075116",
        "entree_sejour_etrangers":        "LEGITEXT000006070158",
        "ceseda":                         "LEGITEXT000006070158",
        "immigration":                    "LEGITEXT000006070158",
        "deontologie_architectes":        "LEGITEXT000006074232",
        "legion_honneur":                 "LEGITEXT000037673300",
        "communes":                       "LEGITEXT000006070162",
        "communes_nouvelle_caledonie":    "LEGITEXT000006070300",
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

    def _req(self, path: str, method: str = "POST", body: Optional[Any] = None) -> Any:
        import json as _json, time as _time_mod
        url = f"{self.PRODUCTION_BASE_URL}{path}"

        # ── Log de la requête sortante ──────────────────────────────────────
        try:
            payload_str = _json.dumps(body, ensure_ascii=False, indent=2) if body is not None else "(aucun body)"
        except Exception:
            payload_str = str(body)
        # Tronquer les payloads trop longs pour la lisibilité du terminal
        payload_log = payload_str if len(payload_str) <= 1200 else payload_str[:1200] + "\n… (tronqué)"
        logger.info(f"[LÉGIFRANCE →] {method.upper()} {path}")
        logger.info(f"[LÉGIFRANCE →] Payload :\n{payload_log}")

        for attempt in range(3):
            t0 = _time_mod.time()
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
                elapsed = int((_time_mod.time() - t0) * 1000)

                if resp.status_code == 401 and attempt == 0:
                    logger.warning(f"[LÉGIFRANCE ←] 401 Unauthorized — renouvellement du token")
                    self._token = None
                    continue

                if resp.status_code == 429:
                    raise RuntimeError("Limite de débit API Légifrance atteinte (429)")

                # ── 500 : log complet pour diagnostiquer ───────────────────
                if resp.status_code == 500:
                    try:
                        err_body = resp.json()
                        msg = (err_body.get("message")
                               or err_body.get("error")
                               or err_body.get("detail")
                               or str(err_body)[:300])
                    except Exception:
                        msg = resp.text[:300] if resp.text else "(corps vide)"
                    logger.error(f"[LÉGIFRANCE ←] 500 sur {path} ({elapsed} ms)")
                    logger.error(f"[LÉGIFRANCE ←] Réponse API : {msg}")
                    logger.error(f"[LÉGIFRANCE ←] Payload qui a causé le 500 :\n{payload_log}")
                    raise RuntimeError(
                        f"Erreur 500 (requête invalide) sur {path} — "
                        f"API : {msg}\n"
                        f"Vérifiez les paramètres : fond (enum exact), filtre_facette, "
                        f"filtre_valeurs, tri (doit correspondre au fond)."
                    )

                resp.raise_for_status()
                ct = resp.headers.get("Content-Type", "")
                data = resp.json() if "application/json" in ct else {"content": resp.text}

                # ── Log de la réponse réussie ──────────────────────────────
                if isinstance(data, dict):
                    total  = data.get("totalResultNumber", "")
                    nb_res = len(data.get("results", []))
                    extra  = f" — {total} résultat(s) total, {nb_res} retourné(s)" if total != "" else ""
                else:
                    extra = ""
                logger.info(f"[LÉGIFRANCE ←] {resp.status_code} {path} ({elapsed} ms){extra}")

                return data

            except httpx.HTTPStatusError as e:
                elapsed = int((_time_mod.time() - t0) * 1000)
                status  = e.response.status_code
                logger.error(f"[LÉGIFRANCE ←] {status} {path} ({elapsed} ms) tentative {attempt+1}/3")
                if status < 500 or status in (502, 503, 504):
                    if status < 500 or attempt == 2:
                        raise RuntimeError(f"Erreur HTTP {status} sur {path}")
                    time.sleep(2 ** attempt)
                else:
                    raise RuntimeError(f"Erreur HTTP {status} sur {path}")
            except httpx.RequestError as e:
                elapsed = int((_time_mod.time() - t0) * 1000)
                logger.error(f"[LÉGIFRANCE ←] Erreur réseau sur {path} ({elapsed} ms) : {e}")
                if attempt == 2:
                    raise RuntimeError(f"Erreur réseau : {e}")
                time.sleep(2 ** attempt)

        raise RuntimeError(f"Échec après 3 tentatives sur {path}")

    def resolve_code(self, code: str) -> str:
        if code.upper().startswith("LEGITEXT"):
            return code.upper()

        import unicodedata

        def _norm(s: str) -> str:
            """Normalise une chaîne : minuscules, sans accents, espaces/tirets → _,
            supprime le préfixe 'code_', 'le_', 'du_', 'de_', 'des_', 'de_la_', 'de_l_'."""
            s = s.lower().strip()
            # Suppression des accents
            s = unicodedata.normalize("NFD", s)
            s = "".join(c for c in s if unicodedata.category(c) != "Mn")
            # Normalisation des séparateurs
            s = s.replace("-", "_").replace(" ", "_").replace("'", "_").replace("'", "_")
            # Suppression des articles/prépositions courants en début
            for prefix in ("code_general_", "code_du_", "code_de_la_", "code_de_l_",
                           "code_des_", "code_de_", "code_", "le_", "la_", "les_",
                           "du_", "de_la_", "de_l_", "des_", "de_"):
                if s.startswith(prefix):
                    s = s[len(prefix):]
                    break
            return s

        key = _norm(code)
        # 1. Correspondance exacte
        if key in self.CODE_IDS:
            return self.CODE_IDS[key]
        # 2. Correspondance partielle : cherche une clé qui contient tous les mots de la query
        words = [w for w in key.split("_") if len(w) > 3]
        if words:
            for dict_key, lid in self.CODE_IDS.items():
                if all(w in dict_key for w in words):
                    return lid
        # 3. Correspondance inverse : une clé qui est contenue dans la query
        for dict_key, lid in self.CODE_IDS.items():
            if dict_key in key and len(dict_key) > 5:
                return lid

        known = "\n".join(f"  • {k}" for k in sorted(self.CODE_IDS.keys()))
        return (
            f"Code non reconnu : '{code}'.\n"
            f"Codes disponibles :\n{known}\n"
            f"Ou passez directement un identifiant LEGITEXT..."
        )


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
    """Formate les résultats de /search en markdown lisible pour le LLM.

    Affiche pour chaque résultat : nature, NOR, numéro, date, ID/CID, numéro
    de parution JO, et les extraits resumePrincipal pour confirmer la pertinence.
    """
    results = data.get("results", [])
    total   = data.get("totalResultNumber", 0)

    if not results:
        hint = (
            "\n\n💡 **Suggestions si aucun résultat avec filtres actifs :**\n"
            "- Essayer sans `filtre_facette`/`filtre_valeurs` pour vérifier si des résultats existent\n"
            "- Essayer `type_recherche='TOUS_LES_MOTS_DANS_UN_CHAMP'` pour une expression exacte multi-mots\n"
            "- Essayer `type_recherche='EXACTE'` pour un nom propre ou un organisme précis\n"
            "- Vérifier les valeurs NATURE valides : ARRETE, DECRET, LOI, ORDONNANCE, DECISION, AVIS, CIRCULAIRE, NOMINATION\n"
            "- Élargir ou reformuler la query (ex: abréviations, sigles)"
        )
        return f"Aucun résultat pour : « {query} »{hint}"

    lines = [f"**{total} résultat(s) au total** — {len(results)} affiché(s)\n"]

    for i, r in enumerate(results, 1):
        # --- Titre ---
        t_list = r.get("titles", [{}]) or [{}]
        title  = t_list[0].get("title", "") or r.get("text", "") or "Sans titre"
        title  = title[:150] + ("…" if len(title) > 150 else "")

        # --- Identifiants ---
        cid = t_list[0].get("cid", "") or r.get("jorfText", "") or t_list[0].get("id", "")

        # --- Métadonnées (sur SearchResult directement) ---
        nature   = r.get("nature", "") or t_list[0].get("nature", "")
        nor      = r.get("nor", "")
        num      = r.get("num", "")
        num_par  = r.get("numParution", "")
        date_sig = r.get("dateSignature", "")
        date_pub = r.get("datePublication", r.get("date", r.get("dateDiffusion", "")))
        etat     = r.get("etat", "") or t_list[0].get("legalStatus", "")

        # --- Extraits textuels (cruciales pour confirmer la pertinence) ---
        resumes = r.get("resumePrincipal", []) or []
        autres  = r.get("autreResume", [])   or []
        extrait = " | ".join(str(s) for s in (resumes + autres)[:3] if s)
        extrait = extrait[:300] + ("…" if len(extrait) > 300 else "")

        # --- Construction ---
        lines.append(f"**{i}.** {title}")
        meta = []
        if nature:     meta.append(f"Nature : {nature}")
        if nor:        meta.append(f"NOR : `{nor}`")
        if num:        meta.append(f"N° {num}")
        if date_sig:   meta.append(f"Signé le {date_sig[:10]}")
        elif date_pub: meta.append(f"Publié le {date_pub[:10]}")
        if num_par:    meta.append(f"JO n°{num_par}")
        if etat:       meta.append(f"État : {etat}")
        if meta:       lines.append("   " + " · ".join(meta))
        if cid:        lines.append(f"   ID : `{cid}`")
        if extrait:    lines.append(f"   *{extrait}*")
        lines.append("")

    return "\n".join(lines)


def _fmt_texte_integral(data: Dict, label: str, max_articles: int = 200) -> str:
    """Formate le contenu intégral d'un texte LEGI (ConsultTextResponse) en parcourant
    récursivement sections et articles, avec le texte complet de chaque article.
    Utilisé par legifrance_code_complet, legifrance_code_par_ancien_id et legifrance_legi_part.
    """
    if not data:
        return f"Aucun contenu trouvé pour {label}."
    titre = data.get("title", label)
    lines = [f"# {titre}\n"]
    etat = data.get("etat", "")
    if etat:
        lines.append(f"**État** : {etat}")
    compteur = {"n": 0}

    def walk(node: Dict, depth: int = 0) -> None:
        node_title = node.get("title", node.get("titre", ""))
        if node_title and depth > 0:
            lines.append(f"\n{'#' * min(depth + 1, 6)} {node_title}")
        for art in node.get("articles", []) or []:
            if compteur["n"] >= max_articles:
                continue
            compteur["n"] += 1
            lines.append(_fmt_article(art))
            lines.append("")
        for sect in node.get("sections", []) or []:
            walk(sect, depth + 1)

    walk(data, 0)
    if compteur["n"] == 0:
        return f"Aucun article trouvé pour {label}."
    if compteur["n"] >= max_articles:
        lines.append(
            f"\n*… limite de {max_articles} articles atteinte, document tronqué. "
            f"Utilisez legifrance_consulter_code pour naviguer section par section.*"
        )
    return "\n".join(lines)


def _fmt_liens_list(items: List[Dict], titre_section: str) -> List[str]:
    """Formate une liste de liens légers (ArticleDTO ou LiensRelatifsDTO :
    champs name/id/cid/cidText/nature/dateDebut/dateVigeur)."""
    lines: List[str] = []
    if not items:
        return lines
    lines.append(f"\n**{titre_section}** ({len(items)})")
    for it in items[:20]:
        nom    = it.get("name", "")
        rid    = it.get("cid", it.get("id", it.get("cidText", "")))
        nature = it.get("nature", "")
        date_v = it.get("dateVigeur", it.get("dateDebut", ""))
        meta   = " | ".join(filter(None, [nature, date_v]))
        line   = f"  - **{nom}**" if nom else "  - (sans titre)"
        if meta: line += f" ({meta})"
        if rid:  line += f" — `{rid}`"
        lines.append(line)
    if len(items) > 20:
        lines.append(f"  *… {len(items)-20} élément(s) supplémentaire(s)*")
    return lines


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
# RESOURCES MCP — Documentation (contexte injecté automatiquement dans le LLM)
# ===========================================================================

@mcp.resource("legifrance://documentation/fonds")
def doc_fonds() -> str:
    """Fonds de recherche Légifrance disponibles et leurs usages."""
    return """
# FONDS LÉGIFRANCE

⚠️ **IMPORTANT : utiliser la valeur exacte du fond** (ex: `CODE_ETAT`, pas `CODE`).
Un fond invalide provoque une erreur 500 de l'API.

| Fond | Description | Valeur exacte |
|------|-------------|--------------|
| Journal Officiel | Textes publiés au JO | **JORF** |
| Codes — état juridique actuel | Articles en vigueur/abrogés à ce jour | **CODE_ETAT** |
| Codes — version à une date | Articles tels qu'ils existaient à une date | **CODE_DATE** |
| Lois, Ordonnances, Décrets — état | LODA version par état juridique | **LODA_ETAT** |
| Lois, Ordonnances, Décrets — date | LODA version à une date donnée | **LODA_DATE** |
| Jurisprudence judiciaire | Cour de cassation, cours d'appel, TGI… | **JURI** |
| Jurisprudence administrative | Conseil d'État, CAA, TA | **CETAT** |
| Jurisprudence financière | Cour des comptes, chambres régionales | **JUFI** |
| Jurisprudence constitutionnelle | Conseil Constitutionnel | **CONSTIT** |
| Conventions collectives | Accords de branche, IDCC | **KALI** |
| Accords d'entreprise | | **ACCO** |
| Circulaires et instructions | | **CIRC** |
| Délibérations CNIL | | **CNIL** |
| Tous les fonds | Recherche transverse | **ALL** |

**Valeurs invalides fréquentes → remplacées automatiquement :**
- `CODE` → `CODE_ETAT`
- `LODA` → `LODA_ETAT`
- `JO` → `JORF`
- `CONVENTION` → `KALI`
- `CIRCULAIRES` → `CIRC`

Pour CODE_* et LODA_* : préférer `_ETAT` pour l'état juridique actuel, `_DATE` pour une version historique.
"""


@mcp.resource("legifrance://documentation/champs-recherche")
def doc_champs_recherche() -> str:
    """Champs de recherche (typeChamp) disponibles par fond."""
    return """
# CHAMPS DE RECHERCHE PAR FOND

Le paramètre `champ` de `legifrance_rechercher` contrôle le champ ciblé.

| Champ | Description | Fonds compatibles |
|-------|-------------|-------------------|
| **ALL** | Tous les champs (défaut) | Tous |
| **TITLE** | Titre du texte | Tous |
| **ARTICLE** | Contenu des articles | JORF, CODE_*, LODA_*, KALI |
| **NUM_ARTICLE** | Numéro d'article | JORF, CODE_*, LODA_* |
| **NOR** | Numéro d'ordre réglementaire | JORF, LODA_*, CIRC, CNIL |
| **NUM** | Numéro du texte | JORF, LODA_* |
| **VISA** | Visas (textes cités) | JORF, LODA_* |
| **NOTICE** | Notice | JORF, LODA_* |
| **VISA_NOTICE** | Visas + notice | JORF, LODA_* |
| **TRAVAUX_PREP** | Travaux préparatoires | JORF, LODA_* |
| **SIGNATURE** | Signataires | JORF, LODA_* |
| **NOTA** | Nota | JORF, LODA_* |
| **TABLE** | Table des matières | CODE_* |
| **TEXTE** | Texte complet | CONSTIT, CETAT, CNIL |
| **ABSTRATS** | Abstracts / résumés | CETAT, JURI, JUFI |
| **RESUMES** | Résumés | CETAT, JURI |
| **NUM_DEC** | Numéro de décision | CONSTIT, CETAT, JUFI |
| **NUM_AFFAIRE** | Numéro d'affaire | JURI |
| **IDCC** | Numéro IDCC | KALI, ACCO |
| **MOTS_CLES** | Mots-clés | KALI |
| **RAISON_SOCIALE** | Raison sociale | ACCO |
| **NUM_DELIB** | Numéro de délibération | CNIL |
| **RESUME_CIRC** | Résumé de circulaire | CIRC |
| **TEXTE_REF** | Texte de référence | CIRC |
"""


@mcp.resource("legifrance://documentation/types-recherche")
def doc_types_recherche() -> str:
    """Types de recherche (typeRecherche) disponibles."""
    return """
# TYPES DE RECHERCHE

Le paramètre `type_recherche` de `legifrance_rechercher` contrôle la logique de correspondance.

| Valeur | Description | Usage recommandé |
|--------|-------------|-----------------|
| **EXACTE** | Expression exacte (entre guillemets) | Rechercher une formulation précise : "force majeure", "abus de droit" |
| **TOUS_LES_MOTS_DANS_UN_CHAMP** | Tous les mots doivent être présents (ET) | Recherche précise multi-mots : responsabilité délictuelle civile |
| **UN_DES_MOTS** | Au moins un mot (OU) — défaut | Recherche large : licenciement rupture résiliation |
| **AUCUN_DES_MOTS** | Aucun des mots ne doit apparaître | Exclusion : ajouter en combinaison avec d'autres critères |
| **AUCUNE_CORRESPONDANCE_A_CETTE_EXPRESSION** | Expression exacte exclue | Exclure une formulation précise |

**Conseil** : utiliser `EXACTE` pour les termes juridiques figés, `TOUS_LES_MOTS_DANS_UN_CHAMP` pour des recherches thématiques précises.
"""


@mcp.resource("legifrance://documentation/options-tri")
def doc_options_tri() -> str:
    """Critères de tri disponibles par fond."""
    return """
# CRITÈRES DE TRI PAR FOND

Le paramètre `tri` de `legifrance_rechercher` dépend du fond choisi.

| Fond | Valeurs de tri disponibles |
|------|---------------------------|
| **ALL** | PERTINENCE |
| **JORF** | PERTINENCE, SIGNATURE_DATE_DESC, SIGNATURE_DATE_ASC, PUBLICATION_DATE_DESC, PUBLICATION_DATE_ASC |
| **LODA_DATE / LODA_ETAT** | PERTINENCE, PUBLICATION_DATE_DESC, PUBLICATION_DATE_ASC, SIGNATURE_DATE_DESC, SIGNATURE_DATE_ASC |
| **CODE_DATE / CODE_ETAT** | PERTINENCE |
| **CIRC** | PERTINENCE, SIGNATURE_DATE_DESC, SIGNATURE_DATE_ASC, PUBLI_DATE_DESC, PUBLI_DATE_ASC |
| **CONSTIT / CETAT / JURI / JUFI / ACCO** | PERTINENCE, DATE_DESC, DATE_ASC |
| **KALI** | PERTINENCE, MODIFICATION_DATE_DESC, SIGNATURE_DATE_DESC, SIGNATURE_DATE_ASC |
| **CNIL** | PERTINENCE, DATE_DECISION_DESC, DATE_DECISION_ASC |

Un tri invalide pour le fond est automatiquement remplacé par PERTINENCE.
Un tri secondaire peut être précisé via `second_tri` (mêmes valeurs).
"""


@mcp.resource("legifrance://documentation/filtres-dates")
def doc_filtres_dates() -> str:
    """Filtres de dates disponibles par fond et facettes associées."""
    return """
# FILTRES DE DATES

Format des dates : **YYYY-MM-DD**. `date_debut` et/ou `date_fin` dans `legifrance_rechercher`.

| Fond | Facette de date utilisée |
|------|--------------------------|
| JORF | DATE_PUBLICATION |
| LODA_DATE / LODA_ETAT | DATE_PUBLICATION |
| CODE_DATE / CODE_ETAT | DATE_VERSION |
| CONSTIT / CETAT / JURI / JUFI | DATE_DECISION |
| KALI | DATE_PUBLICATION |
| ACCO | DATE_DIFFUSION |
| CNIL | DATE_DELIB |
| CIRC | DATE_SIGNATURE |
| **ALL** | ⚠️ Aucun filtre de date (non supporté en recherche transverse) |

Note : DATE_PUBLICATION ≠ DATE_DECISION ≠ DATE_SIGNATURE. Pour la jurisprudence, c'est la date de la décision qui compte, pas la date de mise en ligne.
"""


@mcp.resource("legifrance://documentation/filtres-facettes")
def doc_filtres_facettes() -> str:
    """Filtres par facette disponibles par fond (paramètres filtre_facette / filtre_valeurs)."""
    return """
# FILTRES PAR FACETTE

Utilisez `filtre_facette` + `filtre_valeurs` (liste) dans `legifrance_rechercher` pour affiner.
Plusieurs valeurs dans `filtre_valeurs` forment un OU entre elles.

## Exemple concret : décrets JORF de janvier 2024 sur l'environnement
```
legifrance_rechercher(
    query="environnement",
    fond="JORF",
    date_debut="2024-01-01", date_fin="2024-01-31",
    filtre_facette="NATURE", filtre_valeurs=["DECRET"]
)
```

## Valeurs NATURE par fond (cas le plus courant)

**JORF / LODA_*** :
ARRETE, DECRET, LOI, ORDONNANCE, DECISION, AVIS, CIRCULAIRE, NOMINATION,
ACCORD, CONVENTION, RAPPORT, DECLARATION, INSTRUCTION

**CODE_*** : CODE, LOI, ORDONNANCE, DECRET, ARRETE

⚠️ Pour filtrer par NATURE sur un organisme ou un nom propre, toujours utiliser
`type_recherche="TOUS_LES_MOTS_DANS_UN_CHAMP"` — sinon `UN_DES_MOTS` (OU) retourne
des milliers de résultats non pertinents contenant n'importe lequel des mots de la query.

## Autres facettes disponibles par fond

**JORF** : NATURE, NOR, NUM_TEXTE, NUM_ARTICLE, MINISTERE, EMETTEUR, DATE_SIGNATURE, DATE_PUBLICATION

**CODE_* / LODA_*** : NATURE, NOR, NUM_TEXTE, NUM_ARTICLE,
  ARTICLE_LEGAL_STATUS (VIGUEUR, ABROGE, ABROGE_DIFF, VIGUEUR_DIFF, ANNULE, PERIME, TRANSFERE),
  TEXT_LEGAL_STATUS, DATE_VERSION

⚠️ **NOM_CODE (CODE_* uniquement) : ne PAS utiliser** — cette facette n'accepte que des
   noms de codes exacts tels qu'indexés en base PISTE (non documentés, cause systématiquement
   un 500 avec du texte libre). Pour chercher dans un code précis, utiliser à la place :
   `legifrance_consulter_code('cgfp')` → puis `legifrance_obtenir_section(section_id)`.

**CIRC** : DATE_SIGNATURE, DATE_MEL, DATE_MEA, OPPOSABILITE, DOMAINE, MIN_DEPOSANT, MIN_CONCERNE, NOR, REF_PUBLI

**CONSTIT** : NATURE_CONSTIT, TYPE_DECISION, SOLUTION_CONSTIT, DATE_DECISION, NUMERO_DECISION, NOR

**CETAT** : JURIDICTION_NATURE, DATE_DECISION, PUBLICATION_RECUEIL, NUMERO_DECISION, ECLI

**JURI** : JURIDICTION_JUDICIAIRE, CASSATION_TYPE_PUBLICATION_BULLETIN, CASSATION_NATURE_DECISION,
  CASSATION_FORMATION, DATE_DECISION, NUM_AFFAIRE, ECLI

**JUFI** : JURIDICTION_NATURE, DATE_DECISION, NUMERO_DECISION, PUBLICATION_RECUEIL

**KALI** : LEGAL_STATUS, ACTIVITE, DATE_SIGNATURE, DATE_PUBLICATION, IDCC, NOR, CODE_NAF_OU_APE, NUMERO_BO

**ACCO** : THEME, SIGNATAIRE, DATE_SIGNATURE, DATE_DIFFUSION, ACTIVITE_PRINCIPALE, CODE_APE, IDCC, VILLE, CODE_POSTAL, SIRET_RAISON_SOCIALE

**CNIL** : TYPE, NATURE_DELIB, DATE_DELIB, NUMERO_DELIB, NOR

**ALL** : FOND (pour restreindre à un fonds particulier dans la recherche transverse)

⚠️ `filtre_valeurs` est un **tableau** (ex: `["DECRET"]` et non `"DECRET"`).
"""


@mcp.resource("legifrance://documentation/identifiants")
def doc_identifiants() -> str:
    """Formats des identifiants Légifrance par type de document."""
    return """
# IDENTIFIANTS LÉGIFRANCE

| Préfixe | Type | Exemple | Endpoint de consultation |
|---------|------|---------|--------------------------|
| **LEGIARTI** | Article de code ou loi | LEGIARTI000006419290 | legifrance_obtenir_article |
| **LEGISCTA** | Section de code | LEGISCTA000006099561 | legifrance_obtenir_section |
| **LEGITEXT** | Texte LEGI (code, loi) | LEGITEXT000006070721 | legifrance_consulter_code |
| **JORFTEXT** | Texte du JO | JORFTEXT000000571356 | legifrance_consulter_jorf |
| **JORFARTI** | Article du JO | JORFARTI000000571357 | legifrance_obtenir_article |
| **JURITEXT** | Décision de jurisprudence | JURITEXT000007634763 | legifrance_consulter_juri |
| **CNILTEXT** | Délibération CNIL | CNILTEXT000017651945 | legifrance_consulter_cnil |
| **KALITEXT** | Convention collective | KALITEXT000005635807 | legifrance_consulter_kali_texte |
| **KALIARTI** | Article de convention | KALIARTI000005640792 | legifrance_consulter_kali_article |
| **KALICONT** | Conteneur KALI | KALICONT000005635838 | legifrance_consulter_kali_cont |
| **ACCOTEXT** | Accord d'entreprise | ACCOTEXT000005635123 | legifrance_consulter_accord |

Les identifiants CID (chronologiques) restent stables entre versions ; les ID simples varient selon la version en vigueur.
"""


@mcp.resource("legifrance://documentation/codes-disponibles")
def doc_codes() -> str:
    """Codes juridiques disponibles et leurs identifiants LEGITEXT (source officielle DILA)."""
    return """
# CODES JURIDIQUES — Liste complète (79 codes)

La résolution est flexible : "code général de la fonction publique", "cgfp", "général fonction publique" → même résultat.
Passez directement un ID LEGITEXT si le code n'est pas listé.

## Codes les plus utilisés

| Alias court | Nom officiel | LEGITEXT |
|-------------|-------------|---------|
| civil | Code civil | 000006070721 |
| penal | Code pénal | 000006070719 |
| travail | Code du travail | 000006072050 |
| commerce | Code de commerce | 000005634379 |
| consommation | Code de la consommation | 000006069565 |
| procedure_civile | Code de procédure civile | 000006070716 |
| procedure_penale | Code de procédure pénale | 000006071154 |
| sante_publique | Code de la santé publique | 000006072665 |
| education | Code de l'éducation | 000006071191 |
| environnement | Code de l'environnement | 000006074220 |
| securite_sociale | Code de la sécurité sociale | 000006073189 |
| **cgfp** | **Code général de la fonction publique** | **000044416551** |
| cgct / general_collectivites | Code général des collectivités territoriales | 000006070633 |
| cgi / general_impots | Code général des impôts | 000006069577 |
| lpf / procedures_fiscales | Livre des procédures fiscales | 000006069583 |
| cja / justice_administrative | Code de justice administrative | 000006070933 |
| crpa | Code des relations entre le public et l'administration | 000031366350 |
| construction_habitation | Code de la construction et de l'habitation | 000006074096 |
| action_sociale_familles | Code de l'action sociale et des familles | 000006074069 |
| transports | Code des transports | 000023086525 |
| energie | Code de l'énergie | 000023983208 |
| securite_interieure | Code de la sécurité intérieure | 000025503132 |
| defense | Code de la défense | 000006071307 |
| electoral | Code électoral | 000006070239 |
| penitentiaire | Code pénitentiaire | 000045476241 |
| ceseda / entree_sejour_etrangers | Code de l'entrée et du séjour des étrangers | 000006070158 |
| propriete_intellectuelle | Code de la propriété intellectuelle | 000006069414 |
| monetaire_financier | Code monétaire et financier | 000006072026 |
| urbanisme | Code de l'urbanisme | 000006074075 |
| patrimoine | Code du patrimoine | 000006074236 |
| commande_publique | Code de la commande publique | 000037701019 |
| rural / rural_peche_maritime | Code rural et de la pêche maritime | 000022197698 |
| juridictions_financieres | Code des juridictions financières | 000006070249 |
| douanes | Code des douanes | 000006071570 |
| g3p / propriete_personnes_publiques | Code général de la propriété des personnes publiques | 000006070299 |
| route | Code de la route | 000006074228 |
| sport | Code du sport | 000006071318 |
| aviation_civile | Code de l'aviation civile | 000006074234 |

## Autres codes disponibles

artisanat · cinema · communes · communes_nouvelle_caledonie · cgi_annexe_1/2/3/4 ·
deontologie_architectes · discipline_marine_marchande · domaine_etat · domaine_fluvial ·
expropriation · famille_aide_sociale · forestier · impositions_biens_services ·
instruments_monetaires · justice_militaire · justice_penale_mineurs · legion_honneur ·
minier · minier_ancien · mutualite · organisation_judiciaire · patrimoine ·
pensions_civiles_militaires · pensions_invalides_guerre · pensions_marins ·
ports_maritimes · postes_communications · procedures_civiles_execution ·
recherche · rural_ancien · service_national · tourisme · travail_maritime · voirie_routiere

⚠️ IDs corrigés (erreurs dans versions précédentes) :
  penitentiaire → LEGITEXT000045476241 (pas 000043395876)
  aviation_civile → LEGITEXT000006074234 (pas 000006072301)
  rural/rural_peche_maritime → LEGITEXT000022197698 (pas 000006071367 = ancien)
"""


@mcp.resource("legifrance://documentation/workflow-recherche")
def doc_workflow() -> str:
    """Guide de workflow pour la recherche et consultation de textes juridiques."""
    return """
# WORKFLOW DE RECHERCHE LÉGIFRANCE

## ⭐⭐ Chercher des articles dans un code précis (ex: CGFP, Code civil…)

→ **Ne PAS utiliser `filtre_facette="NOM_CODE"`** (cause systématiquement un 500).
→ Workflow recommandé : **TOC + section + articles**

```
# Étape 1 : obtenir la table des matières du code
legifrance_consulter_code("cgfp")
# → retourne les titres/parties avec leurs sectionId (ex: LEGISCTA000044417123)

# Étape 2 : consulter la section thématique identifiée
legifrance_obtenir_section("LEGISCTA000044417123")
# → retourne les articles de la section avec leurs identifiants

# Étape 3 : lire un article par son identifiant
legifrance_obtenir_article("LEGIARTI000046230209")

# Ou directement par numéro (avec normalisation L/R/D automatique)
legifrance_article_par_numero("cgfp", "L52")   # ou "52" : l'outil essaie L52, R52, 52
legifrance_article_par_numero("cgfp", "L88")
```

Alternative si la section est inconnue — recherche large dans tous les codes puis filtre :
```
legifrance_rechercher(query="sanctions fonctionnaires discipline",
    fond="CODE_ETAT", type_recherche="TOUS_LES_MOTS_DANS_UN_CHAMP")
# Les résultats indiquent le code source dans le titre/NOR
```

→ **Toujours commencer par `legifrance_rechercher`**, pas par `legifrance_sommaire_jorf`.

Exemples directs :

```
# Décrets JORF de janvier 2024 sur l'environnement
legifrance_rechercher(query="environnement", fond="JORF",
    date_debut="2024-01-01", date_fin="2024-01-31",
    filtre_facette="NATURE", filtre_valeurs=["DECRET"])

# Arrêtés de nomination à un organisme précis depuis 2020
# ⚠️ Nom d'organisme = TOUS_LES_MOTS_DANS_UN_CHAMP obligatoire, pas UN_DES_MOTS
legifrance_rechercher(
    query="caisse nationale militaire de sécurité sociale",
    fond="JORF", type_recherche="TOUS_LES_MOTS_DANS_UN_CHAMP",
    date_debut="2020-01-01",
    filtre_facette="NATURE", filtre_valeurs=["ARRETE"])

# Nominations d'une personne dans le JO
legifrance_rechercher(
    query="DUPONT Marie-Claire", fond="JORF",
    type_recherche="TOUS_LES_MOTS_DANS_UN_CHAMP",
    date_debut="2024-01-01")

# Lois sur le logement depuis 2020
legifrance_rechercher(query="logement", fond="JORF",
    date_debut="2020-01-01",
    filtre_facette="NATURE", filtre_valeurs=["LOI"])

# Articles en vigueur du Code civil sur la responsabilité
legifrance_rechercher(query="responsabilité", fond="CODE_ETAT",
    filtre_facette="ARTICLE_LEGAL_STATUS", filtre_valeurs=["VIGUEUR"])

# Jurisprudence récente sur le licenciement
legifrance_rechercher(query="licenciement abusif", fond="JURI",
    tri="DATE_DESC", date_debut="2023-01-01")

# Expression juridique figée
legifrance_rechercher(query="force majeure", fond="CIRC", type_recherche="EXACTE")
```

**Règle de choix de `type_recherche` (critique) :**
- Noms propres, noms d'organismes, acronymes → `TOUS_LES_MOTS_DANS_UN_CHAMP`
- Expression juridique figée → `EXACTE`
- Mots-clés thématiques → `UN_DES_MOTS` (défaut, OU logique — attention : très large)

**⚠️ Années dans la query → corrigées automatiquement :**
Ne jamais mettre une année dans `query` avec `UN_DES_MOTS` — elle est traitée comme un
mot-clé et retournerait des millions de résultats. Le serveur détecte et déplace
automatiquement les années vers `date_debut`/`date_fin`. La correction est indiquée dans
les résultats.

```
# ÉVITER (corrigé automatiquement, mais mieux de l'écrire directement)
legifrance_rechercher(query="programmation militaire 2025 2026", fond="JORF")

# PRÉFÉRER (direct, efficace, 1 seul appel)
legifrance_rechercher(query="programmation militaire", fond="JORF",
    date_debut="2025-01-01", date_fin="2026-12-31",
    type_recherche="TOUS_LES_MOTS_DANS_UN_CHAMP")
```

**⚠️ fond='ALL' + filtre_facette → corrigé automatiquement :**
`fond='ALL'` ne supporte que le filtre `FOND`. Toute autre facette (NATURE, NOR…)
provoque un 500. Le serveur rebascule automatiquement sur `fond='JORF'` et l'indique
dans les résultats.

```
# ÉVITER (rebascule sur JORF automatiquement)
legifrance_rechercher(query="...", fond="ALL", filtre_facette="NATURE", filtre_valeurs=["LOI"])

# PRÉFÉRER (direct)
legifrance_rechercher(query="...", fond="JORF", filtre_facette="NATURE", filtre_valeurs=["LOI"])
```

**Valeurs NATURE valides pour JORF :**
`ARRETE`, `DECRET`, `LOI`, `ORDONNANCE`, `DECISION`, `AVIS`, `CIRCULAIRE`,
`NOMINATION`, `ACCORD`, `CONVENTION`, `RAPPORT`, `DECLARATION`, `INSTRUCTION`

⚠️ `legifrance_sommaire_jorf` = liste brute des textes publiés dans le JO à une date précise.
   À n'utiliser que pour "que contient le JO du 15 mars ?", pas pour chercher par thème.

## 2. Consulter un texte ou article identifié (après recherche)
- Article de code/loi → `legifrance_obtenir_article(id)`
- Code (table des matières) → `legifrance_consulter_code(code, date)`
- Code (texte intégral) → `legifrance_code_complet(code, date)` ⚠️ volumineux
- Texte JO → `legifrance_consulter_jorf(text_cid)`
- Jurisprudence judiciaire → `legifrance_consulter_juri(text_id)`
- Convention collective → `legifrance_consulter_kali_texte(text_id)`
- Délibération CNIL → `legifrance_consulter_cnil(text_id)`

## 3. Naviguer dans un code
→ `legifrance_consulter_code` → repérer un `sectionId`
→ `legifrance_obtenir_section(section_id)` → obtenir les articles
→ `legifrance_obtenir_article(article_id)` → texte complet

## 4. Versions successives d'un texte
→ `legifrance_chronologie_texte(text_cid)`
→ `legifrance_chronologie_element(text_cid, element_cid)`

## 5. Suggestions / autocomplétion
→ `legifrance_suggerer(query)` — recherche générale
→ `legifrance_suggerer_convention(query)` — conventions collectives

## 6. Lister des corpus
→ `legifrance_lister_codes`, `legifrance_lister_conventions`, `legifrance_lister_loda`
→ `legifrance_lister_debats_parlementaires`, `legifrance_lister_questions_parlementaires`
→ `legifrance_lister_bocc`, `legifrance_lister_docs_admins`
"""


@mcp.resource("legifrance://documentation/etats-juridiques")
def doc_etats() -> str:
    """États juridiques des articles et textes."""
    return """
# ÉTATS JURIDIQUES (ARTICLE_LEGAL_STATUS / TEXT_LEGAL_STATUS)

| Valeur | Signification |
|--------|---------------|
| **VIGUEUR** | En vigueur à la date de consultation |
| **VIGUEUR_DIFF** | Vigueur différée (entrera en vigueur à une date future) |
| **ABROGE** | Abrogé |
| **ABROGE_DIFF** | Abrogation différée (sera abrogé à une date future) |
| **ANNULE** | Annulé |
| **PERIME** | Périmé |
| **TRANSFERE** | Transféré (renuméroté) |
| **MODIFIE** | Modifié (version intermédiaire) |

Ces valeurs sont utilisables dans `filtre_valeurs` avec `filtre_facette="ARTICLE_LEGAL_STATUS"` ou `"TEXT_LEGAL_STATUS"`.
Par défaut, `legifrance_rechercher` ne filtre pas par état : tous les états sont retournés.
"""


@mcp.resource("legifrance://documentation/pagination-chrono")
def doc_pagination() -> str:
    """Pagination et accès aux chronologies."""
    return """
# PAGINATION ET CHRONOLOGIES

## Pagination dans `legifrance_rechercher`
- `nb_resultats` : 1 à 20 (défaut : 5). L'API plafonne à 20 par page.
- Pour itérer, relancer avec des `filtre_facette` / `date_debut` / `date_fin` différents.

## Chronologie d'un texte
- `legifrance_chronologie_texte(text_cid)` → liste de toutes les versions d'un texte (JORF, lois, codes)
- `legifrance_chronologie_element(text_cid, element_cid)` → versions d'un article ou section précis

## Tables annuelles du JO
- `legifrance_tables_annuelles(annee_fin, annee_debut)` → index annuel des textes publiés
- `legifrance_annees_sans_table()` → années sans table disponible
- `legifrance_dates_sans_jo()` → jours sans publication du JO

## Versions canoniques (redirections)
- `legifrance_version_canonique_article(article_id)` → version en vigueur d'un article
- `legifrance_version_canonique_texte(text_id)` → version canonique d'un texte
- `legifrance_version_proche(text_id)` → version la plus proche d'une date
"""


# ===========================================================================
# OUTILS MCP — Recherche (4)
# ===========================================================================

# Valeurs valides pour le paramètre 'fond' (enum swagger SearchRequestDTO)
_FONDS_VALIDES: set = {
    "JORF", "CNIL", "CETAT", "JURI", "JUFI", "CONSTIT",
    "KALI", "CODE_DATE", "CODE_ETAT", "LODA_DATE", "LODA_ETAT",
    "ALL", "CIRC", "ACCO",
}

# Aliases courants envoyés par les LLM → valeur correcte
_FOND_ALIASES: Dict[str, str] = {
    "CODE":            "CODE_ETAT",   # le plus fréquent
    "CODES":           "CODE_ETAT",
    "LODA":            "LODA_ETAT",
    "LEGI":            "LODA_ETAT",
    "JO":              "JORF",
    "JOURNAL_OFFICIEL":"JORF",
    "JURISPRUDENCE":   "JURI",
    "JUDICIAIRE":      "JURI",
    "ADMINISTRATIF":   "CETAT",
    "ADMINISTRATIF_":  "CETAT",
    "CONSEIL_ETAT":    "CETAT",
    "CONSTITUTIONNEL": "CONSTIT",
    "CONVENTIONS":     "KALI",
    "CONVENTION":      "KALI",
    "CIRCULAIRES":     "CIRC",
    "ACCORDS":         "ACCO",
    "FINANCIER":       "JUFI",
    "FINANCIERE":      "JUFI",
}

# Le fond "ALL" (recherche transverse) ne documente aucun filtre de date.
_DATE_FACETTE_PAR_FOND: Dict[str, str] = {
    "JORF":      "DATE_PUBLICATION",
    "CODE_DATE": "DATE_VERSION",
    "CODE_ETAT": "DATE_VERSION",
    "LODA_DATE": "DATE_PUBLICATION",
    "LODA_ETAT": "DATE_PUBLICATION",
    "CONSTIT":   "DATE_DECISION",
    "CETAT":     "DATE_DECISION",
    "JURI":      "DATE_DECISION",
    "JUFI":      "DATE_DECISION",
    "KALI":      "DATE_PUBLICATION",
    "ACCO":      "DATE_DIFFUSION",
    "CNIL":      "DATE_DELIB",
    "CIRC":      "DATE_SIGNATURE",
}

# Champs de recherche (typeChamp) disponibles par fond
# (cf. onglets JORF, CODE, LODA, Circulaires… du XLSX).
# Tous les fonds supportent au minimum ALL et TITLE.
_CHAMP_PAR_FOND: Dict[str, List[str]] = {
    "ALL":       ["ALL", "TITLE"],
    "JORF":      ["ALL", "TITLE", "NOR", "NUM", "NUM_ARTICLE", "ARTICLE",
                  "VISA", "NOTICE", "VISA_NOTICE", "TRAVAUX_PREP", "SIGNATURE", "NOTA"],
    "CODE_DATE": ["ALL", "TITLE", "TABLE", "NUM_ARTICLE", "ARTICLE"],
    "CODE_ETAT": ["ALL", "TITLE", "TABLE", "NUM_ARTICLE", "ARTICLE"],
    "LODA_DATE": ["ALL", "TITLE", "NOR", "NUM", "NUM_ARTICLE", "ARTICLE",
                  "VISA", "NOTICE", "VISA_NOTICE", "TRAVAUX_PREP", "SIGNATURE", "NOTA"],
    "LODA_ETAT": ["ALL", "TITLE", "NOR", "NUM", "NUM_ARTICLE", "ARTICLE",
                  "VISA", "NOTICE", "VISA_NOTICE", "TRAVAUX_PREP", "SIGNATURE", "NOTA"],
    "CIRC":      ["ALL", "TITLE", "NOR", "RESUME_CIRC", "TEXTE_REF"],
    "CONSTIT":   ["ALL", "TITLE", "NUM_DEC", "TEXTE"],
    "CETAT":     ["ALL", "TITLE", "NUM_DEC", "ABSTRATS", "TEXTE", "RESUMES"],
    "JURI":      ["ALL", "TITLE", "ABSTRATS", "NUM_AFFAIRE", "TEXTE", "RESUMES"],
    "JUFI":      ["ALL", "TITLE", "NUM_DEC", "ABSTRATS", "TEXTE"],
    "KALI":      ["ALL", "TITLE", "IDCC", "MOTS_CLES", "ARTICLE"],
    "ACCO":      ["ALL", "TITLE", "RAISON_SOCIALE", "IDCC"],
    "CNIL":      ["ALL", "TITLE", "NOR", "TEXTE", "NUM_DELIB"],
}

# Critères de tri valides par fond.
# Utilisés pour valider le paramètre 'tri' et documenter les options possibles.
_SORT_PAR_FOND: Dict[str, List[str]] = {
    "ALL":       ["PERTINENCE"],
    "JORF":      ["PERTINENCE", "SIGNATURE_DATE_DESC", "SIGNATURE_DATE_ASC",
                  "PUBLICATION_DATE_DESC", "PUBLICATION_DATE_ASC"],
    "CODE_DATE": ["PERTINENCE"],
    "CODE_ETAT": ["PERTINENCE"],
    "LODA_DATE": ["PERTINENCE", "PUBLICATION_DATE_DESC", "PUBLICATION_DATE_ASC",
                  "SIGNATURE_DATE_DESC", "SIGNATURE_DATE_ASC"],
    "LODA_ETAT": ["PERTINENCE", "PUBLICATION_DATE_DESC", "PUBLICATION_DATE_ASC",
                  "SIGNATURE_DATE_DESC", "SIGNATURE_DATE_ASC"],
    "CIRC":      ["PERTINENCE", "SIGNATURE_DATE_DESC", "SIGNATURE_DATE_ASC",
                  "PUBLI_DATE_DESC", "PUBLI_DATE_ASC"],
    "CONSTIT":   ["PERTINENCE", "DATE_DESC", "DATE_ASC"],
    "CETAT":     ["PERTINENCE", "DATE_DESC", "DATE_ASC"],
    "JURI":      ["PERTINENCE", "DATE_DESC", "DATE_ASC"],
    "JUFI":      ["PERTINENCE", "DATE_DESC", "DATE_ASC"],
    "KALI":      ["PERTINENCE", "MODIFICATION_DATE_DESC",
                  "SIGNATURE_DATE_DESC", "SIGNATURE_DATE_ASC"],
    "ACCO":      ["PERTINENCE", "DATE_DESC", "DATE_ASC"],
    "CNIL":      ["PERTINENCE", "DATE_DECISION_DESC", "DATE_DECISION_ASC"],
}


@mcp.tool()
def legifrance_rechercher(
    query: str,
    nb_resultats: int = 5,
    fond: str = "ALL",
    date_debut: Optional[str] = None,
    date_fin: Optional[str] = None,
    tri: str = "PERTINENCE",
    second_tri: Optional[str] = None,
    champ: str = "ALL",
    type_recherche: str = "UN_DES_MOTS",
    operateur: str = "ET",
    filtre_facette: Optional[str] = None,
    filtre_valeurs: Optional[List[str]] = None,
) -> str:
    """Recherche de textes juridiques français dans Légifrance.

    Interroge codes, lois, décrets, arrêtés, circulaires, jurisprudence,
    conventions collectives et Journal Officiel. Retourne titres, natures et identifiants.

    Args:
        query: Mots-clés (ex: 'responsabilité civile', 'licenciement abusif', 'RGPD').
        nb_resultats: Nombre de résultats (1–20, défaut: 5).
        fond: Fonds à interroger. Valeurs : ALL (tous), JORF, CODE_DATE, CODE_ETAT,
              LODA_DATE, LODA_ETAT, JURI, JUFI, CETAT, CONSTIT, KALI, CNIL, CIRC, ACCO.
              → Voir ressource legifrance://documentation/fonds
        date_debut: Date minimale YYYY-MM-DD. Sans effet si fond='ALL'.
        date_fin: Date maximale YYYY-MM-DD. Sans effet si fond='ALL'.
              → Voir ressource legifrance://documentation/filtres-dates
        tri: Critère de tri principal. Dépend du fond.
             Valeurs courantes : PERTINENCE, SIGNATURE_DATE_DESC, SIGNATURE_DATE_ASC,
             PUBLICATION_DATE_DESC, PUBLICATION_DATE_ASC, DATE_DESC, DATE_ASC,
             DATE_DECISION_DESC, DATE_DECISION_ASC, MODIFICATION_DATE_DESC.
             → Voir ressource legifrance://documentation/options-tri
        second_tri: Critère de tri secondaire (départage à égalité). Mêmes valeurs que tri.
        champ: Champ ciblé (typeChamp). Dépend du fond.
               Valeurs : ALL, TITLE, ARTICLE, NOR, NUM, NUM_ARTICLE, TEXTE, ABSTRATS,
               RESUMES, IDCC, MOTS_CLES, RAISON_SOCIALE, NUM_DELIB, RESUME_CIRC…
               → Voir ressource legifrance://documentation/champs-recherche
        type_recherche: Logique de correspondance sur la query.
               - EXACTE : expression exacte (ex : "force majeure")
               - TOUS_LES_MOTS_DANS_UN_CHAMP : tous les mots présents (ET strict)
               - UN_DES_MOTS : au moins un mot (OU) — défaut
               - AUCUN_DES_MOTS : exclusion de tous les mots
               - AUCUNE_CORRESPONDANCE_A_CETTE_EXPRESSION : exclusion de l'expression exacte
               → Voir ressource legifrance://documentation/types-recherche
        operateur: Opérateur logique global entre champs : ET (défaut) ou OU.
        filtre_facette: Facette de filtre non-date (ex : 'NATURE', 'NOR',
               'JURIDICTION_NATURE', 'ETAT_TEXTE', 'NUM_TEXTE', 'IDCC',
               'ARTICLE_LEGAL_STATUS', 'TEXT_LEGAL_STATUS'…).
               → Voir ressource legifrance://documentation/filtres-facettes
        filtre_valeurs: Liste de valeurs pour filtre_facette.
               Plusieurs valeurs forment un OU entre elles (ex: ['LOI', 'DECRET']).
               Exemples : ['LOI'], ['VIGUEUR'], ['DECRET', 'ARRETE'].
               → Voir ressource legifrance://documentation/etats-juridiques
    """
    c = _get_client()
    try:
        query = query.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    # --- Validation du fond ---
    fond_upper = fond.upper().strip()
    if fond_upper not in _FONDS_VALIDES:
        fond_upper = _FOND_ALIASES.get(fond_upper, "ALL")
        if fond_upper not in _FONDS_VALIDES:
            fond_upper = "ALL"
    fond = fond_upper

    # Valider type_recherche
    types_valides = {
        "EXACTE", "TOUS_LES_MOTS_DANS_UN_CHAMP", "UN_DES_MOTS",
        "AUCUN_DES_MOTS", "AUCUNE_CORRESPONDANCE_A_CETTE_EXPRESSION",
    }
    if type_recherche not in types_valides:
        type_recherche = "UN_DES_MOTS"

    # Valider opérateur
    if operateur not in ("ET", "OU"):
        operateur = "ET"

    # Valider et corriger le tri selon le fond
    sorts_valides = _SORT_PAR_FOND.get(fond, ["PERTINENCE"])
    if tri not in sorts_valides:
        tri = sorts_valides[0]

    # Valider le champ selon le fond
    champs_valides = _CHAMP_PAR_FOND.get(fond, ["ALL", "TITLE"])
    if champ not in champs_valides:
        champ = "ALL"

    recherche: Dict[str, Any] = {
        "champs": [{"typeChamp": champ,
                    "criteres": [{"typeRecherche": type_recherche, "valeur": query, "operateur": operateur}],
                    "operateur": operateur}],
        "operateur":      operateur,
        "pageSize":       min(max(1, nb_resultats), 20),
        "pageNumber":     1,
        "sort":           tri,
        "typePagination": "DEFAUT",
    }

    if second_tri:
        recherche["secondSort"] = second_tri

    filtres: List[Dict[str, Any]] = []
    notes: List[str] = []   # corrections silencieuses à signaler dans le résultat

    # ── Correction silencieuse : fond='ALL' + filtre non-FOND ───────────────
    # Sur fond='ALL', seul le filtre 'FOND' est supporté par l'API — tout autre
    # filtre (NATURE, NOR…) provoque un 500. On rebascule automatiquement sur JORF
    # qui supporte tous les filtres courants et est le fond le plus complet pour
    # les recherches par NATURE (LOI, DECRET, ARRETE…).
    if filtre_facette and fond == "ALL" and filtre_facette != "FOND":
        notes.append(
            f"ℹ️ `fond='ALL'` est incompatible avec `filtre_facette='{filtre_facette}'` "
            f"(l'API ne supporte que le filtre FOND en recherche transverse). "
            f"La recherche a été effectuée sur **fond='JORF'** automatiquement."
        )
        fond = "JORF"
        # Recalculer tri et champ pour le nouveau fond
        sorts_valides = _SORT_PAR_FOND.get(fond, ["PERTINENCE"])
        if tri not in sorts_valides:
            tri = sorts_valides[0]
        champs_valides = _CHAMP_PAR_FOND.get(fond, ["ALL", "TITLE"])
        if champ not in champs_valides:
            champ = "ALL"

    # ── Correction silencieuse : années dans la query ───────────────────────
    # Mettre une année dans la query avec UN_DES_MOTS retourne des millions de
    # résultats hors-sujet. On extrait automatiquement les années vers date_debut/date_fin.
    import re as _re
    annees = _re.findall(r'\b(20\d{2})\b', query)
    if annees and not date_debut and not date_fin:
        query_clean = _re.sub(r'\s*\b20\d{2}\b', '', query).strip()
        date_debut  = f"{min(annees)}-01-01"
        date_fin    = f"{max(annees)}-12-31"
        notes.append(
            f"ℹ️ Années détectées dans la query ({', '.join(annees)}) → déplacées vers "
            f"`date_debut='{date_debut}'` / `date_fin='{date_fin}'`. "
            f"Query nettoyée : `\"{query_clean}\"`."
        )
        query = query_clean
        # Avec des dates, TOUS_LES_MOTS_DANS_UN_CHAMP est plus pertinent que UN_DES_MOTS
        if type_recherche == "UN_DES_MOTS":
            type_recherche = "TOUS_LES_MOTS_DANS_UN_CHAMP"
            notes.append("ℹ️ `type_recherche` automatiquement ajusté à `TOUS_LES_MOTS_DANS_UN_CHAMP`.")

    # ── Filtre date ──────────────────────────────────────────────────────────
    if date_debut or date_fin:
        facette = _DATE_FACETTE_PAR_FOND.get(fond)
        if facette:
            dates: Dict[str, str] = {}
            if date_debut: dates["start"] = date_debut
            if date_fin:   dates["end"]   = date_fin
            filtres.append({"facette": facette, "dates": dates})

    # ── Filtre non-date ──────────────────────────────────────────────────────
    if filtre_facette and filtre_valeurs:
        vals = filtre_valeurs if isinstance(filtre_valeurs, list) else [filtre_valeurs]
        filtres.append({"facette": filtre_facette, "valeurs": vals})

    if filtres:
        recherche["filtres"] = filtres

    # Mettre à jour la query dans la recherche après corrections éventuelles
    recherche["champs"][0]["criteres"][0]["valeur"]        = query
    recherche["champs"][0]["criteres"][0]["typeRecherche"] = type_recherche

    data    = c._req("/search", body={"fond": fond, "recherche": recherche})
    total   = data.get("totalResultNumber", 0) if isinstance(data, dict) else 0
    result  = _fmt_search(data, query)

    # ── Alerte si résultats trop larges ─────────────────────────────────────
    if total > 50_000:
        notes.append(
            f"⚠️ {total:,} résultats — requête trop large. Affiner avec : "
            f"`type_recherche='TOUS_LES_MOTS_DANS_UN_CHAMP'`, `date_debut`/`date_fin`, "
            f"ou `filtre_facette='NATURE'` avec un fond spécifique."
        )

    if notes:
        result = "\n".join(f"> {n}" for n in notes) + "\n\n" + result
    return result


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
def legifrance_consulter_code(code: str, date: Optional[str] = None, nature: Optional[str] = None) -> str:
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
        nature: Nature du texte pour filtrer la recherche. Valeurs possibles :
                CODE, DECRET, ARRETE, LOI, ORDONNANCE, etc. (optionnel).
    """
    c = _get_client()
    tid = c.resolve_code(code)
    if not tid.startswith("LEGITEXT"):
        return tid
    body: Dict[str, Any] = {
        "textId": tid,
        "date":   _date_today.today().isoformat() if date is None else date,
    }
    if nature:
        body["nature"] = nature
    data = c._req("/consult/legi/tableMatieres", body=body)
    return _fmt_toc(data, code)


@mcp.tool()
def legifrance_code_complet(code: str, date: Optional[str] = None) -> str:
    """Récupère le contenu complet d'un code juridique avec le texte intégral de ses articles.

    Attention : réponse potentiellement volumineuse (limitée à 200 articles). Préférez
    legifrance_consulter_code puis legifrance_obtenir_article pour une navigation ciblée.

    Args:
        code: Nom ou ID LEGITEXT du code.
        date: Date de consultation YYYY-MM-DD (défaut: aujourd'hui).
    """
    c = _get_client()
    tid = c.resolve_code(code)
    if not tid.startswith("LEGITEXT"):
        return tid
    data = c._req("/consult/code", body={
        "textId": tid,
        "date":   _date_today.today().isoformat() if date is None else date,
    })
    return _fmt_texte_integral(data, code)


@mcp.tool()
def legifrance_code_par_ancien_id(ancien_id: str) -> str:
    """Consulte un code par son ancien identifiant Légifrance.

    Utile pour résoudre des références héritées vers des identifiants actuels.

    Args:
        ancien_id: Ancien identifiant du code.
    """
    c    = _get_client()
    data = c._req("/consult/getCodeWithAncienId", body={"ancienId": ancien_id})
    return _fmt_texte_integral(data, ancien_id)


@mcp.tool()
def legifrance_obtenir_article(article_id: str) -> str:
    """Récupère le texte complet d'un article juridique par son identifiant Légifrance.

    L'ID (format LEGIARTI000...) s'obtient via legifrance_rechercher ou legifrance_consulter_code.

    Args:
        article_id: Identifiant Légifrance de l'article (ex: LEGIARTI000006419283).
    """
    c    = _get_client()
    data = c._req("/consult/getArticle", body={"id": article_id})
    return _fmt_article(data.get("article", data))


@mcp.tool()
def legifrance_article_par_numero(code: str, numero: str) -> str:
    """Récupère un article d'un code par son numéro.

    Accepte les numéros avec ou sans préfixe législatif/réglementaire :
    '52', 'L52', 'L 52', 'l52', 'R52', 'D52' sont tous valides.
    Pour le CGFP et les codes récents, les articles sont préfixés L (législatif)
    ou R/D (réglementaire) ; l'outil essaie automatiquement les variantes.

    Args:
        code: Nom du code (cgfp, civil, travail…) ou ID LEGITEXT.
              Exemples : 'cgfp', 'general_fonction_publique', 'civil', 'travail'.
        numero: Numéro de l'article.
                Exemples : '52', 'L52', 'L1234-5', 'R10', 'D3', 'L2121-1'.
    """
    c   = _get_client()
    tid = c.resolve_code(code)
    if not tid.startswith("LEGITEXT"):
        return tid  # message d'erreur retourné par resolve_code

    # Normalisation et construction des variantes à essayer
    raw = numero.strip().upper().replace(" ", "")
    # Variantes : essaie le format fourni, puis avec/sans préfixe L/R/D
    if raw and raw[0] in ("L", "R", "D") and raw[1:].replace("-", "").isdigit():
        # Format préfixé fourni → essayer aussi sans préfixe
        variants = [raw, raw[1:]]
    elif raw.replace("-", "").isdigit():
        # Numéro pur → essayer L-préfixé (codes législatifs), R-préfixé, et brut
        variants = [f"L{raw}", f"R{raw}", raw]
    else:
        variants = [raw]

    last_err = ""
    for num in variants:
        try:
            data    = c._req("/consult/getArticleWithIdAndNum", body={"id": tid, "num": num})
            article = data.get("article", data)
            if article and article.get("id"):
                result = _fmt_article(article)
                if result and "Contenu non disponible" not in result:
                    return result
        except RuntimeError as e:
            last_err = str(e)
            continue

    # Aucune variante n'a fonctionné
    tried = ", ".join(f"'{v}'" for v in variants)
    return (
        f"Article non trouvé (variantes essayées : {tried}) dans le code `{code}`.\n"
        f"Conseil : utilisez `legifrance_consulter_code('{code}')` pour obtenir la table "
        f"des matières et repérer l'identifiant exact de l'article.\n"
        + (f"Dernière erreur : {last_err}" if last_err else "")
    )


@mcp.tool()
def legifrance_article_par_eli(eli: str) -> str:
    """Récupère un article par son identifiant ELI (European Legislation Identifier).

    L'ELI est le format d'URI standardisé européen pour identifier les textes législatifs
    (ex: eli/loi/2016/10/7/2016-1321/jo/texte).

    Args:
        eli: Identifiant ELI de l'article.
    """
    c    = _get_client()
    data = c._req("/consult/getArticleWithIdEliOrAlias", body={"idEliOrAlias": eli})
    return _fmt_article(data.get("article", data))


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
def legifrance_articles_meme_numero(
    text_cid: str,
    article_cid: str,
    numero: str,
    date: Optional[str] = None,
) -> str:
    """Récupère les anciens/nouveaux textes ayant porté le même numéro qu'un article donné.

    Utile pour retrouver les renumérotations d'un article au fil des révisions d'un texte.

    Args:
        text_cid: CID chronologique du texte contenant l'article (ex: LEGITEXT000006070721).
        article_cid: CID chronologique de l'article de référence.
        numero: Numéro de l'article (ex: '1382', 'L1234-5').
        date: Date de référence YYYY-MM-DD (défaut: aujourd'hui).
    """
    c = _get_client()
    body: Dict[str, Any] = {
        "textCid":    text_cid,
        "articleCid": article_cid,
        "articleNum": numero,
        "date":       _date_today.today().isoformat() if date is None else date,
    }
    data = c._req("/consult/sameNumArticle", body=body)
    new_texts = data.get("newTexts", [])
    old_texts = data.get("oldTexts", [])
    if not new_texts and not old_texts:
        return f"Aucun texte partageant le numéro '{numero}' n'a été trouvé."
    lines = [f"**Articles ayant porté le numéro '{numero}'**"]
    lines.extend(_fmt_liens_list(new_texts, "Nouveaux textes"))
    lines.extend(_fmt_liens_list(old_texts, "Anciens textes"))
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
    body: Dict[str, Any] = {
        "textId": part_id,
        "date":   _date_today.today().isoformat() if date is None else date,
    }
    data = c._req("/consult/legiPart", body=body)
    return _fmt_texte_integral(data, part_id)


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
    """Récupère les liens de concordance d'un article (anciens/nouveaux textes équivalents).

    Args:
        article_id: Identifiant de l'article (LEGIARTI...).
    """
    c    = _get_client()
    data = c._req("/consult/concordanceLinksArticle", body={"articleId": article_id})
    new_texts = data.get("newTexts", [])
    old_texts = data.get("oldTexts", [])
    if not new_texts and not old_texts:
        return f"Aucun lien de concordance pour `{article_id}`."
    lines = [f"**Liens de concordance de `{article_id}`**"]
    lines.extend(_fmt_liens_list(new_texts, "Nouveaux textes"))
    lines.extend(_fmt_liens_list(old_texts, "Anciens textes"))
    return "\n".join(lines)


@mcp.tool()
def legifrance_liens_relatifs(article_id: str) -> str:
    """Récupère les liens relatifs (citants/cités) d'un article juridique.

    Args:
        article_id: Identifiant de l'article (LEGIARTI...).
    """
    c    = _get_client()
    data = c._req("/consult/relatedLinksArticle", body={"articleId": article_id})
    cite     = data.get("liensCite", [])
    cite_par = data.get("liensCitePar", [])
    if not cite and not cite_par:
        return f"Aucun lien relatif trouvé pour `{article_id}`."
    lines = [f"**Liens relatifs de `{article_id}`**"]
    lines.extend(_fmt_liens_list(cite, "Cite (cet article cite)"))
    lines.extend(_fmt_liens_list(cite_par, "Cité par (éléments qui citent cet article)"))
    return "\n".join(lines)


@mcp.tool()
def legifrance_liens_service_public(article_id: str) -> str:
    """Récupère les liens vers service-public.fr associés à un article.

    Args:
        article_id: Identifiant de l'article (CID).
    """
    c    = _get_client()
    data = c._req("/consult/servicePublicLinksArticle", body={"articleCid": article_id})
    liens = data.get("liensSP", {}) or {}
    if not liens:
        return f"Aucun lien service-public.fr pour `{article_id}`."
    lines = [f"**{len(liens)} lien(s) service-public.fr**\n"]
    for k, v in liens.items():
        if str(v).startswith("http"):
            lines.append(f"- [{k}]({v})")
        elif str(k).startswith("http"):
            lines.append(f"- [{v}]({k})")
        else:
            lines.append(f"- {k} : {v}")
    return "\n".join(lines)


@mcp.tool()
def legifrance_a_liens_service_public(article_id: str) -> str:
    """Vérifie si un article possède des liens vers service-public.fr.

    Args:
        article_id: Identifiant de l'article (CID).
    """
    c    = _get_client()
    data = c._req("/consult/hasServicePublicLinksArticle", body=[article_id])
    ids_avec_liens = data if isinstance(data, list) else []
    if article_id in ids_avec_liens:
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
    """Résout un alias ELI et retourne le contenu complet du texte du JO correspondant.

    Args:
        eli: Identifiant ELI ou alias (ex: eli/loi/2016/10/7/2016-1321/jo/texte).
    """
    c    = _get_client()
    data = c._req("/consult/eliAndAliasRedirectionTexte", body={"idEliOrAlias": eli})
    if not data or not data.get("id"):
        return f"Aucun texte trouvé pour l'ELI/alias : `{eli}`"
    titre  = data.get("title", eli)
    cid    = data.get("cid", "")
    date_p = data.get("dateParution", "")
    nature = data.get("nature", "")
    lines  = [f"# {titre}\n"]
    if cid:     lines.append(f"**CID** : `{cid}`")
    if nature:  lines.append(f"**Nature** : {nature}")
    if date_p:  lines.append(f"**Publication JO** : {date_p}")
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
            lines.append(f"*… {len(all_arts)-50} article(s) supplémentaires*")
    return "\n".join(lines)


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
    """Consulte le sommaire structuré du Journal Officiel sur une date ou une période.

    ⚠️  Cet outil liste les textes PUBLIÉS dans le JO par date — il ne fait PAS de recherche
    par contenu, thème ou mots-clés. Pour chercher des décrets sur l'environnement, le
    logement, le travail etc., utiliser `legifrance_rechercher` avec fond='JORF'.

    Cas d'usage de cet outil :
    - "Qu'y a-t-il dans le JO du 15 mars 2026 ?" → date='2026-03-15'
    - "Quels textes ont été publiés dans le JO entre le 1er et le 5 janvier 2024 ?" → date + date_fin
    - Naviguer un conteneur JORF connu par son identifiant JORFCONT...

    Pour filtrer par nature (décret, loi, arrêté) ET par thème → utiliser legifrance_rechercher
    avec filtre_facette='NATURE', filtre_valeurs=['DECRET'] et fond='JORF'.

    Args:
        date: Date de début YYYY-MM-DD (ex: '2026-03-01').
        date_fin: Date de fin YYYY-MM-DD (ex: '2026-03-31'). Si omis, recherche sur date seule.
        jorf_id: Identifiant conteneur JORF (ex: JORFCONT000049456397). Prioritaire sur date.
        recherche: Texte à rechercher dans les titres du sommaire.
        nb_resultats: Nombre de résultats (défaut: 20, max: 100).
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


def _ts_ms_to_date(ts: Any) -> str:
    """Convertit un timestamp Légifrance (millisecondes depuis epoch) en YYYY-MM-DD.
    Retourne une chaîne vide si la valeur est absente, nulle ou non convertible.
    """
    if not ts:
        return ""
    try:
        import datetime as _dt
        ms = int(str(ts))
        if ms <= 0 or ms > 9_999_999_999_999:   # valeur sentinelle 2999… → vide
            return ""
        return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return str(ts)[:10]


def _fmt_juri(txt: Dict, avec_texte_integral: bool = False) -> str:
    """Formate un objet TexteSimple (décision de jurisprudence) en fiche structurée
    optimisée pour la synthèse et les tableaux comparatifs.

    Champs prioritaires pour la synthèse :
      solution · sommaire · motsCles · formation · publicationRecueil · ecli
    """
    titre        = txt.get("titre", txt.get("title", "Décision sans titre"))
    # Juridiction — préférer le champ détaillé
    juri         = txt.get("juridiction", "")
    juri_nat     = txt.get("natureJuridiction", "")
    juri_jud     = txt.get("juridictionJudiciaire", "")
    # Date — timestamp ms
    date_str     = _ts_ms_to_date(txt.get("dateTexte") or txt.get("relevantDate") or txt.get("datePubliComputed"))
    # Identifiants
    num_aff      = txt.get("numeroAffaire", []) or []
    if isinstance(num_aff, str): num_aff = [num_aff]
    ecli         = txt.get("ecli", "")
    rid          = txt.get("id", txt.get("cid", ""))
    # Décision
    solution     = txt.get("solution", "")
    formation    = txt.get("formation", "")
    publi        = txt.get("publicationRecueil", "") or txt.get("typePublicationBulletin", "")
    # Résumés / mots-clés
    sommaire_raw = txt.get("sommaire", []) or []
    mots_cles    = txt.get("motsCles", []) or []
    titrages     = txt.get("titragesKey", txt.get("titrages", [])) or []
    # Texte intégral
    texte_html   = _strip_html(txt.get("texteHtml", txt.get("texte", "")))

    lines = [f"# {titre}\n"]

    # ── Métadonnées structurées ──────────────────────────────────────────────
    if juri:        lines.append(f"**Juridiction** : {juri}")
    if juri_nat and juri_nat != juri:
                    lines.append(f"**Nature** : {juri_nat}")
    if juri_jud and juri_jud not in (juri, juri_nat):
                    lines.append(f"**Juridiction judiciaire** : {juri_jud}")
    if date_str:    lines.append(f"**Date** : {date_str}")
    if num_aff:     lines.append(f"**N° affaire** : {' · '.join(num_aff)}")
    if formation:   lines.append(f"**Formation** : {formation}")
    if solution:    lines.append(f"**Solution** : {solution}")
    if publi:       lines.append(f"**Publication** : {publi}")
    if ecli:        lines.append(f"**ECLI** : {ecli}")
    if rid:         lines.append(f"**ID** : `{rid}`")

    # ── Mots-clés ────────────────────────────────────────────────────────────
    if mots_cles:
        mc_str = ", ".join(str(m) for m in mots_cles[:15] if m)
        if mc_str: lines.append(f"**Mots-clés** : {mc_str}")

    # ── Sommaire ─────────────────────────────────────────────────────────────
    if sommaire_raw:
        lines.append("\n## Sommaire")
        for item in sommaire_raw:
            if isinstance(item, dict):
                # extraire le texte quel que soit le champ
                t = item.get("texte", item.get("text", item.get("titre", str(item))))
            else:
                t = str(item)
            if t: lines.append(f"- {t}")

    # ── Titrages ─────────────────────────────────────────────────────────────
    if titrages and not sommaire_raw:
        tit_str = " | ".join(str(t.get("id", t) if isinstance(t, dict) else t) for t in titrages[:5] if t)
        if tit_str: lines.append(f"\n**Titrages** : {tit_str}")

    # ── Texte intégral (optionnel) ────────────────────────────────────────────
    if avec_texte_integral and texte_html:
        lines.append("\n## Texte intégral")
        limit = 8000
        lines.append(texte_html[:limit])
        if len(texte_html) > limit:
            lines.append(f"\n*[… {len(texte_html)-limit} caractères supplémentaires non affichés]*")
    elif not sommaire_raw and texte_html:
        # Pas de sommaire → extraire un chapeau minimal pour la synthèse
        lines.append("\n## Extrait")
        lines.append(texte_html[:1500])
        if len(texte_html) > 1500:
            lines.append(f"\n*[… utilisez `avec_texte_integral=True` pour le texte complet]*")

    return "\n".join(lines)


# ===========================================================================
# OUTILS MCP — Consultation jurisprudence (3)
# ===========================================================================

@mcp.tool()
def legifrance_jurisprudence(text_id: str, avec_texte_integral: bool = False) -> str:
    """Consulte une décision de jurisprudence française et retourne une fiche structurée.

    La fiche est optimisée pour la synthèse et les tableaux comparatifs : elle expose
    directement la **solution** (cassation, rejet…), le **sommaire** officiel, les
    **mots-clés**, la **formation**, la **publication** au bulletin et l'**ECLI**.

    ⚠️ Cet outil retourne déjà le sommaire et la solution — il n'est pas nécessaire
    d'appeler un outil séparé pour obtenir un résumé ou un commentaire.

    Args:
        text_id: Identifiant de la décision (ex: JURITEXT000037999394).
        avec_texte_integral: Si True, inclut le texte complet (≤ 8 000 car.).
                             Laisser False pour les synthèses et tableaux comparatifs.
    """
    c    = _get_client()
    data = c._req("/consult/juri", body={"textId": text_id})
    txt  = data.get("text") or {}
    if isinstance(txt, list):
        txt = txt[0] if txt else {}
    if not txt:
        return f"Aucune décision trouvée pour l'identifiant : `{text_id}`"
    return _fmt_juri(txt, avec_texte_integral=avec_texte_integral)


@mcp.tool()
def legifrance_jurisprudence_plan_classement(
    libelle: Optional[str] = None,
    fond: Optional[str] = None,
    niveau: Optional[int] = None,
    juri_id: Optional[str] = None,
    page: Optional[int] = None,
    search_suggest: bool = False,
    search_by_niveau: bool = False,
) -> str:
    """Consulte le plan de classement de la jurisprudence (arborescence thématique).

    Permet de naviguer dans les catégories de jurisprudence par thème ou matière.

    Args:
        libelle: Libellé à rechercher (ex: 'contrat', 'responsabilité').
        fond: Fond juridictionnel (ex: 'juri', 'CASS', 'CE').
        niveau: Niveau hiérarchique dans l'arborescence (0 = racine).
        juri_id: Identifiant JURINOME d'un nœud précis (ex: JURINOME000007644451).
        page: Numéro de page de la requête (défaut: 1).
        search_suggest: Si True, active la recherche par suggestion (défaut: False).
        search_by_niveau: Si True, recherche par niveau plutôt que par libellé (défaut: False).
    """
    c    = _get_client()
    body: Dict[str, Any] = {
        "searchSuggest":  search_suggest,
        "searchByNiveau": search_by_niveau,
    }
    if libelle is not None:  body["libelle"] = libelle
    if fond is not None:     body["fond"]    = fond
    if niveau is not None:   body["niveau"]  = niveau
    if juri_id is not None:  body["id"]      = juri_id
    if page is not None:     body["page"]    = page
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
def legifrance_jurisprudence_ancien_id(ancien_id: str, avec_texte_integral: bool = False) -> str:
    """Consulte une décision de jurisprudence par son ancien identifiant.

    Args:
        ancien_id: Ancien identifiant de la décision.
        avec_texte_integral: Si True, inclut le texte complet (≤ 8 000 car.).
    """
    c    = _get_client()
    data = c._req("/consult/getJuriWithAncienId", body={"ancienId": ancien_id})
    txt  = data.get("text") or {}
    if isinstance(txt, list):
        txt = txt[0] if txt else {}
    if not txt:
        return f"Aucune décision trouvée pour l'identifiant : `{ancien_id}`"
    return _fmt_juri(txt, avec_texte_integral=avec_texte_integral)


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
    c    = _get_client()
    data = c._req("/consult/getCnilWithAncienId", body={"ancienId": ancien_id})
    # L'API renvoie ConsultCnilTextResponse = {text: {...}, executionTime, dereferenced}
    txt  = data.get("text") or {}
    if not txt:
        return f"Aucune délibération CNIL trouvée pour l'identifiant : `{ancien_id}`"
    titre = txt.get("titre", txt.get("title", ancien_id))
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
    tri: str = "DEBAT_PARLEMENTAIRE_DESC",
) -> str:
    """Liste les débats parlementaires disponibles dans Légifrance.

    Args:
        types_publication: Types de publication pour filtrer
            (ex: ['ASSEMBLEE_NATIONALE'], ['SENAT']).
        date_parution: Date de parution YYYY-MM-DD.
        nb_resultats: Nombre de résultats (défaut: 20).
        tri: Critère de tri. Valeurs : DEBAT_PARLEMENTAIRE_DESC (défaut),
             DEBAT_PARLEMENTAIRE_ASC, ID_ASC, ID_DESC.
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats, "sort": tri}
    if types_publication: body["facetteTypePublication"] = types_publication
    if date_parution:     body["dateParution"]           = date_parution
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
    tri: str = "QUESTION_ECRITE_PARLEMENTAIRE_DESC",
) -> str:
    """Liste les questions écrites parlementaires disponibles dans Légifrance.

    Args:
        parlement_types: Types de parlement (['AN'] pour Assemblée Nationale, ['SENAT']).
        periode_publication: Période de publication (ex: '01/01/2024 > 31/12/2024').
        nb_resultats: Nombre de résultats (défaut: 20).
        tri: Critère de tri. Valeurs : QUESTION_ECRITE_PARLEMENTAIRE_DESC (défaut),
             QUESTION_ECRITE_PARLEMENTAIRE_ASC, ID_ASC, ID_DESC.
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats, "sort": tri}
    if parlement_types:     body["parlementTypes"]         = parlement_types
    if periode_publication: body["periodePublication"]     = periode_publication
    data  = c._req("/list/questionsEcritesParlementaires", body=body)
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
    idcc: Optional[str] = None,
    nb_resultats: int = 20,
    tri: str = "BOCC_SORT_DESC",
) -> str:
    """Liste les numéros du BOCC (Bulletin Officiel des Conventions Collectives).

    Args:
        interval_publication: Intervalle de publication (ex: '01/01/2024 > 31/12/2024').
        idcc: Numéro IDCC pour filtrer (ex: '0016').
        nb_resultats: Nombre de résultats (défaut: 20).
        tri: Critère de tri. Valeurs : BOCC_SORT_DESC (défaut), BOCC_SORT_ASC.
    """
    c    = _get_client()
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats, "sort": tri}
    if interval_publication: body["intervalPublication"] = interval_publication
    if idcc:                 body["idcc"]                = idcc
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
def legifrance_lister_docs_admins(
    annees: Optional[List[int]] = None,
    nb_resultats: int = 20,
) -> str:
    """Liste les documents administratifs disponibles dans Légifrance pour une ou plusieurs années.

    Args:
        annees: Liste d'années à filtrer (ex: [2023, 2024]).
        nb_resultats: Nombre de résultats (défaut: 20).
    """
    c    = _get_client()
    # Le filtre d'années s'appelle 'facetteYearPubli' (facette YEARS) dans l'API.
    body: Dict[str, Any] = {"pageNumber": 1, "pageSize": nb_resultats}
    if annees: body["facetteYearPubli"] = annees
    data  = c._req("/list/docsAdmins", body=body)
    items = data.get("results", [])
    if not items:
        return "Aucun document administratif trouvé."
    total = data.get("totalResultNumber", len(items))
    lines = [f"**{total} document(s) administratif(s)** — {len(items)} affiché(s)\n"]
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
    # Le schéma BodmrListRequest attend 'years' (tableau), pas 'year' (scalaire).
    if annee: body["years"] = [annee]
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
def legifrance_dates_sans_jo(annee: Optional[int] = None) -> str:
    """Retourne les dates sans Journal Officiel connues de l'API Légifrance.

    L'API renvoie toutes les dates disponibles (sans filtre par année côté serveur).
    Si 'annee' est fourni, le filtrage est effectué localement.

    Args:
        annee: Année à filtrer localement (ex: 2024). Optionnel.
    """
    c    = _get_client()
    # /misc/datesWithoutJo est un GET sans body ni paramètre de requête.
    data  = c._req("/misc/datesWithoutJo", method="GET")
    dates = data.get("lstDateDisabled", [])
    if not dates:
        return "Aucune date sans JO disponible."
    if annee:
        dates = [d for d in dates if str(annee) in str(d)]
    if not dates:
        return f"Aucune date sans JO pour {annee}."
    label = f"en {annee}" if annee else "(toutes années)"
    lines = [f"**{len(dates)} date(s) sans Journal Officiel {label}**\n"]
    for d in dates[:100]:
        lines.append(f"- {d}")
    if len(dates) > 100:
        lines.append(f"*… {len(dates)-100} date(s) supplémentaires*")
    return "\n".join(lines)


@mcp.tool()
def legifrance_annees_sans_table() -> str:
    """Retourne les années sans table annuelle du Journal Officiel disponible dans Légifrance.

    Utile avant d'appeler legifrance_tables_annuelles.
    """
    c     = _get_client()
    # /misc/yearsWithoutTable est un GET sans body.
    data  = c._req("/misc/yearsWithoutTable", method="GET")
    annees = data.get("lstYearDisabled", [])
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
