# Veilleuse — contexte pour Claude Code

## Ce que c'est
Babyphone collectif pour une fête en gîte (40 ans de Sylvain, 3 octobre 2026). Les enfants dorment
dans des chalets à 100-300 m de la salle, la musique est forte. Chaque couple laisse un téléphone
(avec SIM) dans le chalet en mode « émetteur » et garde l'autre à la salle en mode « récepteur ».
Le PC de la sono affiche aussi le tableau en plein écran (mode `?mode=sono`).

## Principes de conception (ne pas casser)
- **Événements, pas streaming.** La détection de bruit se fait sur le téléphone du chalet ;
  seuls de petits messages transitent (heartbeat 15 s, alerte, clip audio 4 s ≤ 150 Ko en bonus).
  Ça doit marcher en 3G faible.
- **Le silence est une alerte.** Sans heartbeat pendant 45 s → « chalet muet » affiché partout.
- **Alerte non acquittée 90 s → escalade** rouge clignotante sur tous les récepteurs.
- **Aucun compte.** Accès par lien privé signé (le fragment `#...` est la clé de la soirée).
- **Persistance minimale, jamais d'audio** (évolution décidée par Sylvain, sept. 2026) :
  `VEILLEUSE_STATE_FILE` (JSON local, volume `/srv/data` en Docker) garde soirées, chalets
  attendus, alertes sans clip et abonnements push pour survivre aux redémarrages. Les clips
  audio restent en mémoire seule.
- **Zéro dépendance front** : HTML/CSS/JS vanilla dans `app/static/`. Ne pas introduire de
  framework ou de bundler.
- Interface en français, ton simple et chaleureux.

## Structure
- `app/main.py` — FastAPI + WebSocket `/ws/{code}`, modèle `Party`/`Chalet`, watchdog 2 s.
  Protocole documenté dans le README (section « Protocole WebSocket »).
- `app/static/index.html`, `app.css`, `app.js` — les trois modes (chalet / salle / sono).
- `tests/test_backend.py` — pytest (logique métier + flux WebSocket via TestClient).
- `Dockerfile`, `docker-compose.yml` — déploiement Coolify, port 8000.

## Commandes
- Lancer : `uvicorn app.main:app --reload`
- Tests : `pip install -r requirements-dev.txt && pytest`
- Test navigateur avec micro simulé (Playwright/Chromium) : voir `docs/e2e.py`
  (`python docs/e2e.py`, serveur lancé sur :8000). Il produit les captures de `docs/`.

## Points d'architecture à ne pas casser (appris à la dure)
- Le service worker est **servi à la racine** (`/sw.js`, route FastAPI) : depuis `/static/`
  sa portée n'aurait couvert que `/static/` et le push serait mort-né (bug réel, un mois).
- Jamais d'attente sans délai sur `serviceWorker.ready` (voir `swReady()` dans app.js).
- Les envois push et les diffusions WebSocket sont **hors du chemin critique**
  (`schedule_pushes`, broadcast parallèle borné à 3 s par socket).
- Chaque alerte porte un `aid` ; les actions humaines ne se mettent jamais en file hors ligne.
- `hello` ne donne jamais le rôle émetteur ; reprendre un chalet exige son jeton (`token`).
- Endpoints push limités aux vrais services (FCM/APNs/Mozilla/WNS) — anti-SSRF.

## État au 11 septembre 2026
Trois revues externes intégralement traitées. 41 tests backend, batteries navigateur
(parcours, reprises, redémarrage réel, muet/escalade/rappel/renfort). Toujours **jamais
validé sur un vrai iPhone** ; Android testé partiellement par Sylvain (Pixel 9).

## Prochaines étapes
1. Répétition terrain (checklist dans la conversation) : iPhone + Android, écran verrouillé,
   appel entrant, 4G coupée, deux « J'y vais » simultanés, redéploiement en pleine soirée.
2. Régler le seuil par défaut et la courbe dB→% d'après les vrais essais.
3. Éventuellement : écoute en direct WebRTC à la demande si le réseau du domaine le permet.
