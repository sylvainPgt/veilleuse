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
- **Aucun compte, aucune persistance.** Un code de soirée, tout en mémoire.
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

## État au 25 août 2026
V1 fonctionnelle, testée avec micro simulé uniquement. Jamais testée sur un vrai téléphone.

## Prochaines étapes envisagées (à valider avec Sylvain)
1. Test réel : deux téléphones, un vrai pleur, couper la 4G 50 s → « chalet muet » puis retour.
2. Vérifier iOS Safari : wake lock, micro au premier plan pendant 1 h, MediaRecorder `audio/mp4`.
3. Régler le seuil par défaut et la courbe dB→% d'après les vrais essais.
4. Éventuellement : planification des rondes, notifications push web (Android), écoute en
   direct WebRTC à la demande si le réseau le permet.
