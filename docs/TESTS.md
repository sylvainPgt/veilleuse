# Ce qui est vérifié, comment, et ce qui ne l'est pas

Quatre niveaux de preuve, du plus fort au plus faible. Ne confondez pas le
troisième avec le quatrième : **une notification mise en file ou inscrite au
journal n'est pas une notification reçue.**

| Niveau | Ce que ça prouve | Où |
|---|---|---|
| 1. Logique serveur | Le modèle métier se comporte comme prévu, y compris aux cas limites. Aucun navigateur, aucun réseau. | `tests/test_backend.py` |
| 2. Interactions navigateur | Un vrai Chromium clique, tape, appuie et relâche sur la vraie interface. | `docs/e2e*.py` |
| 3. Service worker | Le worker est bien enregistré à la racine, contrôle la page, et `ready` se résout. | `docs/e2e_incidents.py`, étape 1 |
| 4. **Livraison push réelle** | **NON VÉRIFIÉ.** Exige un appareil physique et un service Google/Apple. | `docs/CHECKLIST-TERRAIN.md` |

## Lancer

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium     # pour les scénarios navigateur
pytest                                     # niveau 1 — quelques secondes
```

Chaque scénario navigateur **démarre son propre serveur** sur son propre port et
l'arrête en sortant. Rien à lancer à la main, et ils peuvent tourner en
parallèle. Seule exception : `e2e.py`, qui produit les captures du README et
attend un serveur déjà lancé sur `:8000`.

```bash
python docs/e2e_taps.py         # :8793 — interactions réelles pendant les actualisations
python docs/e2e_incidents.py    # :8795 — chalet muet, urgences multiples, renfort, rappel
python docs/e2e_identity.py     # :8796 — jetons par soirée, relève d'émetteur
python docs/e2e_resume.py       # :8797 — reprise après rechargement
python docs/e2e_restart.py      # :8791 — arrêt/redémarrage réel du processus

uvicorn app.main:app --port 8000 &   # e2e.py seul a besoin de ça
python docs/e2e.py                    # captures du README
```

`docs/_harness.py` porte les briques communes (démarrage du serveur, création de
soirée, émetteur WebSocket, helpers de page). Ce n'est pas un scénario.

## Qui couvre quoi

**`e2e_taps.py` — les gestes réels.** Sept scénarios, uniquement des interactions
authentiques : clic souris sans `force`, appui tactile (`tap`), et séquences
appui/changement d'état/relâchement aux coordonnées. **Jamais `dispatch_event`** :
un événement synthétique contourne les contrôles d'actionnabilité et masquerait
le défaut qu'on veut interdire. Couvre « J'y vais » (overlay et tuile),
« C'est réglé », « Renfort », « Je vais vérifier », en souris et au toucher, plus
les deux cas durs : l'ordre des tuiles qui change entre l'appui et le
relâchement, et la tuile pressée qui doit être reconstruite pendant l'appui.
Chaque scénario vérifie d'abord que les durées défilent vraiment — sinon il ne
prouverait rien.

**`e2e_incidents.py` — l'honnêteté des états.** Portée du service worker et
résolution de `ready` ; chalet muet dont l'alerte ne se referme pas toute seule ;
« Je vais vérifier » visible sans faire reverdir la tuile ; retour du heartbeat
qui clôt la vérification ; deux urgences simultanées avec priorité déterministe
et compteur ; bascule vers l'urgence suivante après prise en charge ; renfort et
rappel d'acquittement **audibles sur une page visible** ; perte de connexion
pendant une alerte acquittée, qui reste distincte de la prise en charge.

**`e2e_identity.py` — qui parle pour un chalet.** Aller-retour entre deux soirées
sans tuile dupliquée, jetons cloisonnés par soirée, relève d'émetteur dans les
deux sens avec démission de l'ancienne connexion.

**`e2e_resume.py` — les rechargements.** La salle et le chalet reprennent leur
rôle ; « Retour » efface la reprise ; un lien vers une autre soirée l'emporte.

**`e2e_restart.py` — le redémarrage.** Arrêt et relance réels du processus,
pages laissées ouvertes, reconnexion automatique, et l'assertion qui compte :
une alerte émise **après** le redémarrage atteint la salle.

## Ce qu'aucun de ces tests ne prouve

- **La livraison d'une notification push sur un téléphone.** Les tests vérifient
  que le serveur accepte l'abonnement, met l'envoi en file, et que le service de
  push a accepté la requête. La suite dépend de Google/Apple et de l'appareil.
- **Le micro réel.** Chromium simule une entrée audio. Le seuil, la sensibilité,
  la tenue d'une heure au premier plan et les interruptions (appel, Siri,
  verrouillage) ne se vérifient que sur un vrai téléphone.
- **iOS.** Aucun Safari n'est testé ici, ni son `MediaRecorder`, ni son wake lock,
  ni son comportement en arrière-plan.
- **Le réseau réel.** Pas de 3G faible, pas de bascule Wi-Fi/4G, pas de latence.

Pour ces quatre points : `docs/CHECKLIST-TERRAIN.md`.
