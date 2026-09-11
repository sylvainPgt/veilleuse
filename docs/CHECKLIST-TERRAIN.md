# Validation sur appareils réels

Rien de ce qui suit n'a été vérifié automatiquement : les tests du dépôt tournent
sur un Chromium avec micro simulé. **Tant que ces essais ne sont pas faits, Veilleuse
ne doit pas être présentée comme validée sur appareils.**

Il faut : un iPhone, un Android, le PC de la sono, et le réseau du domaine — ou à
défaut la 4G réelle de chaque téléphone, jamais le Wi-Fi de la maison.

## Avant de commencer — l'hébergement

Sur `/admin`, l'encart « État du déploiement » doit afficher quatre coches :

- `VEILLEUSE_SECRET` défini — sans lui, liens et abonnements push meurent à chaque
  redéploiement ;
- Web Push disponible côté serveur ;
- écriture de l'état `ok` ;
- **volume durable détecté**.

Le dernier point est le seul qui compte vraiment, et c'est aussi celui qu'on
confond le plus volontiers : `persistence: "ok"` signifie *la dernière écriture a
réussi*, rien de plus. Sur un conteneur sans volume monté, l'écriture réussit
aussi — et tout disparaît au redéploiement suivant. La seule preuve :

> créer une soirée de test → **redéployer depuis Coolify** → rouvrir `/admin` :
> la soirée doit toujours y être.

Vérifier enfin qu'il n'y a **qu'une réplique du conteneur et qu'un seul worker**
Uvicorn : l'état vit dans le processus, deux instances se partageraient les
soirées sans se voir.

## Test court (15 min) — à faire d'abord

- [ ] **Android, écran verrouillé.** Activer les alertes, attendre « Push ✓ »,
      appuyer sur « Tester mes notifications », verrouiller l'écran : la
      notification d'essai doit arriver.
- [ ] **Appui sur la notification** → l'app s'ouvre sur **la bonne soirée**, en
      rôle salle. Refaire app complètement fermée.
- [ ] **iPhone.** Safari → Partager → « Sur l'écran d'accueil », rouvrir depuis
      l'icône, puis la même séquence. Sans l'écran d'accueil, iOS refuse le push.
- [ ] **Clap dans le chalet** → la salle sonne, « J'y vais » affiche le prénom sur
      l'écran du chalet. Pas de prénom, pas de preuve.
- [ ] **Sono** : l'alerte s'affiche en géant, avec la durée.

## Essai long (1 h minimum) — sur les téléphones de la soirée

À faire une fois, dans les conditions réelles : téléphone du chalet branché au
chargeur, posé où il sera, porte fermée.

- [ ] **Micro au premier plan, 1 h.** L'écran chalet doit rester sur « À l'écoute »
      du début à la fin. Taper dans les mains à la 55ᵉ minute : la salle doit
      sonner. C'est le test qui compte le plus sur iPhone.
- [ ] **Récepteur verrouillé pendant tout l'essai.** À la fin, vérifier combien
      d'alertes ont été reçues par rapport à celles émises.
- [ ] **Interruption du micro puis reprise.** Passer un appel sur le téléphone du
      chalet, décrocher 30 s, raccrocher. Attendu : « Micro coupé ! » sur place,
      « chalet muet » à la salle, puis retour à « À l'écoute » — automatiquement,
      ou par le bouton « Réactiver le micro ». Noter lequel.
- [ ] **Coupure réseau puis reconnexion.** Mode avion 60 s côté chalet : la salle
      doit afficher « chalet muet » puis la tuile doit reverdir seule. Vérifier
      qu'un appui sur « Je vais vérifier » pendant la coupure **ne fait pas**
      reverdir la tuile.
- [ ] **Silence pendant une prise en charge.** Déclencher une alerte, taper
      « J'y vais », puis couper le réseau du chalet : la salle doit signaler le
      chalet muet **sans** effacer la prise en charge.
- [ ] **Redémarrage serveur avec un chalet qui ne revient pas.** Deux chalets
      connectés, un récepteur avec l'app **fermée**. Redéployer depuis Coolify.
      Rallumer un seul chalet. Attendu : au bout de deux minutes, le récepteur
      reçoit une notification « Chalet muet » pour l'absent — et une seule.
- [ ] **Deux « J'y vais » simultanés** depuis deux téléphones : un seul prénom
      doit s'afficher partout.
- [ ] **Appuis pendant que ça bouge.** Avec deux chalets dont un en alerte (donc
      des tuiles qui se réordonnent), appuyer sur « C'est réglé » et « Renfort » :
      aucun appui ne doit être perdu ni partir sur le mauvais chalet.
- [ ] **Batterie.** Relever le pourcentage du téléphone du chalet au début et à la
      fin : c'est ce qui dira s'il tiendra la nuit sans chargeur de secours.

## À rapporter

Modèle et version d'OS de chaque téléphone, seuil retenu, nombre de fausses
alertes, et pour chaque case cochée ce qui s'est réellement passé — surtout quand
ça diffère de l'attendu.
