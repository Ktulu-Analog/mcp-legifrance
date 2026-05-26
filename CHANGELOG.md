# Changelog

Toutes les modifications notables de ce projet sont documentées dans ce fichier.

Format basé sur [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/).

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
