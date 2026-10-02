/* Veilleuse — webapp (émetteur chalet / récepteur salle / écran sono)
   Vanilla JS, aucune dépendance. */
(() => {
  "use strict";

  // ---------- utilitaires ----------
  const $ = (id) => document.getElementById(id);
  const views = ["home", "chalet-setup", "chalet-run", "salle"];
  const show = (name) => views.forEach((v) => $("view-" + v).classList.toggle("hidden", v !== name));
  const slug = (s) => (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  const store = {
    get: (k, d) => { try { return JSON.parse(localStorage.getItem("veilleuse." + k)) ?? d; } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem("veilleuse." + k, JSON.stringify(v)); } catch { /* privé */ } },
  };
  // Mémoire de l'onglet : survit au rechargement, pas à la fermeture — la bonne
  // portée pour « reprendre son rôle » sans ressusciter une vieille session.
  const tab = {
    get: (k, d) => { try { return JSON.parse(sessionStorage.getItem("veilleuse." + k)) ?? d; } catch { return d; } },
    set: (k, v) => { try { sessionStorage.setItem("veilleuse." + k, JSON.stringify(v)); } catch { /* privé */ } },
    del: (k) => { try { sessionStorage.removeItem("veilleuse." + k); } catch { /* ignore */ } },
  };
  let toastTimer;
  const toast = (msg, ms = 2500) => { const t = $("toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), ms); };
  const fmtAgo = (s) => s < 60 ? `${Math.max(0, Math.round(s))} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60).toString().padStart(2, "0")}`;
  const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const params = new URLSearchParams(location.search);
  const isSono = params.get("mode") === "sono";
  if (isSono) document.body.classList.add("sono");

  // ---------- session ----------
  const session = { code: "", name: "", role: "" };

  // ---------- connexion WebSocket robuste (reconnexion + file hors ligne) ----------
  const net = {
    ws: null, queue: [], backoff: 1000, onState: null, onMessage: null, onConn: null, onUnknown: null,
    open: false, hello: null, timer: null, gen: 0, lastMsgAt: 0, lastRev: 0,
    connect() {
      clearTimeout(this.timer);
      const gen = ++this.gen;   // toute socket d'une génération passée est ignorée
      const proto = location.protocol === "https:" ? "wss" : "ws";
      let ws;
      try { ws = new WebSocket(`${proto}://${location.host}/ws/${encodeURIComponent(session.code)}`); } catch { return this.retry(); }
      this.ws = ws;
      const mine = () => gen === this.gen;
      ws.onopen = () => {
        if (!mine()) return ws.close();
        // Nouvelle socket = nouveau compteur : après un redémarrage le serveur repart
        // à rev=1, et un lastRev hérité ferait ignorer tous les états. Le garde `gen`
        // suffit contre les messages d'une ancienne socket ; la révision ne protège
        // que l'ordre au sein de la connexion en cours.
        this.lastRev = 0;
        this.open = true; this.backoff = 1000; this.lastMsgAt = Date.now();
        // register/hello d'abord : un heartbeat parti avant serait ignoré par le
        // serveur et le chalet resterait « jamais connecté » quinze secondes.
        if (this.hello) ws.send(JSON.stringify(this.hello));
        this.onConn?.(true);
        // rejoue ce qui n'a pas pu partir (alertes en priorité, on garde l'ordre)
        const q = this.queue.splice(0); q.forEach((m) => ws.send(JSON.stringify(m)));
        if (q.length) toast(`Connexion rétablie, ${q.length} message(s) renvoyé(s)`);
      };
      ws.onmessage = (e) => {
        if (!mine()) return;
        let m; try { m = JSON.parse(e.data); } catch { return; }
        this.lastMsgAt = Date.now();
        if (m.type === "unknown_party") return this.onUnknown?.();
        // deux diffusions peuvent se chevaucher : on n'affiche jamais un état plus vieux
        if (m.type === "state" && m.rev) {
          if (m.rev <= this.lastRev) return;
          this.lastRev = m.rev;
        }
        this.onMessage?.(m);
      };
      ws.onclose = () => { if (!mine()) return; this.open = false; this.onConn?.(false); this.retry(); };
      ws.onerror = () => { try { ws.close(); } catch { /* ignore */ } };
    },
    retry() { this.timer = setTimeout(() => this.connect(), this.backoff); this.backoff = Math.min(this.backoff * 1.7, 15000); },
    send(msg, { queueIfOffline = true } = {}) {
      if (this.open && this.ws?.readyState === 1) { this.ws.send(JSON.stringify(msg)); return true; }
      if (queueIfOffline) { this.queue.push(msg); if (this.queue.length > 50) this.queue.splice(0, this.queue.length - 50); }
      return false;
    },
    close() {
      // Fermeture voulue (éteindre, changer de soirée) : incrémenter la génération
      // rend l'ancienne socket muette — sans ça, son onclose relançait une
      // reconnexion fantôme qui ré-enregistrait le chalet dans l'ancienne soirée.
      this.gen++; clearTimeout(this.timer);
      this.onConn = null; this.onMessage = null; this.onUnknown = null;
      this.queue.length = 0; this.hello = null; this.lastRev = 0;
      try { this.ws?.close(); } catch { /* ignore */ } this.open = false;
    },
  };
  setInterval(() => { if (net.open) net.send({ type: "ping" }, { queueIfOffline: false }); }, 25000);

  // ---------- wake lock (garde l'écran allumé) ----------
  let wakeLock = null;
  async function keepAwake() {
    try { wakeLock = await navigator.wakeLock?.request("screen"); return !!wakeLock; } catch { return false; }
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (wakeLock !== null) keepAwake();
    // Retour au premier plan : le navigateur peut avoir suspendu les AudioContext
    // en douce — celui du micro comme celui de la sonnerie de la salle.
    if (detector.ctx && detector.ctx.state === "suspended") detector.ctx.resume().catch(() => {});
    if (salle.audio && salle.audio.state === "suspended") salle.audio.resume().then(updateSalleStatus).catch(() => {});
  });

  // ---------- accueil ----------
  $("in-name").value = store.get("name", "");
  let chosenRole = "";
  let createMode = false;

  // L'identifiant vit dans le fragment (#...) : il ne part donc ni dans les journaux
  // du serveur ni dans l'en-tête Referer quand quelqu'un suit un lien depuis la page.
  // On accepte aussi bien le lien nu que le message de partage entier collé.
  const codeFromLink = (v) => {
    const s = String(v || "").trim();
    const all = s.match(/#([a-z0-9-]{4,})/gi);
    if (all) return all[all.length - 1].slice(1).toLowerCase();
    const bare = s.match(/^([a-z0-9-]+)$/i);
    return bare ? bare[1].toLowerCase() : "";
  };
  const linkFor = (code) => `${location.origin}/#${code}`;

  // Seuls les liens connus sur ce téléphone sont affichés : jamais de catalogue
  // serveur qui divulguerait les clés privées des autres soirées.
  const recent = {
    all() {
      const list = store.get("recent", []);
      if (!Array.isArray(list)) return [];
      return list.filter((p) => p && typeof p.code === "string" && /^[a-z0-9-]{4,}$/.test(p.code))
        .filter((p, i, valid) => valid.findIndex((v) => v.code === p.code) === i);
    },
    add(code, name) {
      // Le code, pas le nom, identifie la soirée : deux soirées peuvent partager un nom.
      const n = name || code;
      const list = recent.all().filter((p) => p.code !== code);
      list.unshift({ code, name: n, ts: Date.now() });
      store.set("recent", list);
    },
    forget(code) { store.set("recent", recent.all().filter((p) => p.code !== code)); },
    clear() { store.set("recent", []); },
  };

  // On ne demande que ce que le rôle choisi rend nécessaire : le prénom ne sert
  // qu'aux récepteurs (il s'affiche dans « X y va »), le chalet n'en a pas besoin.
  const ROLE_STEP = {
    chalet: { label: "Le téléphone du chalet", name: false, cta: "Préparer la veilleuse" },
    salle: { label: "Le téléphone qui vient danser", name: true, cta: "Voir les chalets" },
    sono: { label: "L'écran de la sono", name: false, cta: "Afficher le tableau" },
  };
  document.querySelectorAll("#form-home [data-role]").forEach((b) => b.addEventListener("click", () => {
    chosenRole = b.dataset.role;
    const step = ROLE_STEP[chosenRole];
    document.querySelectorAll("#form-home [data-role]").forEach((o) => {
      o.classList.toggle("selected", o === b);
      o.setAttribute("aria-pressed", o === b ? "true" : "false");
    });
    $("home-role-label").textContent = step.label;
    $("lab-name").classList.toggle("hidden", !step.name);
    $("in-name").required = step.name;              // sinon un champ caché bloque l'envoi
    $("btn-continue").textContent = step.cta;
    $("home-step2").classList.remove("hidden");
    loadParties();
    if (!$("in-code").value) $("in-code").focus();
    else if (step.name && !$("in-name").value) $("in-name").focus();
  }));

  function loadParties() {
    const list = recent.all();
    $("home-parties").classList.toggle("hidden", !list.length);
    $("party-chips").innerHTML = list.map((p) =>
      `<span class="chip-wrap"><button type="button" class="btn chip" data-code="${esc(p.code)}">${esc(p.name)}</button>` +
      `<button type="button" class="btn chip-x" data-forget="${esc(p.code)}" aria-label="Oublier ${esc(p.name)}" title="Oublier cette soirée">×</button></span>`).join("");
  }
  loadParties(); // dès l'arrivée, avant même le choix chalet / salle
  // Seules les clés déjà connues sont interrogées ; une suppression serveur
  // retire la puce, mais une panne réseau ne fait rien oublier.
  async function refreshParties() {
    for (const p of recent.all()) {
      try {
        const r = await fetch(`/api/party/${encodeURIComponent(p.code)}`);
        if (!recent.all().some((known) => known.code === p.code)) continue;
        if (r.status === 404) { recent.forget(p.code); loadParties(); }
        else if (r.ok) {
          const state = await r.json();
          if (state.name && recent.all().some((known) => known.code === p.code && known.name !== state.name)) {
            store.set("recent", recent.all().map((known) => known.code === p.code ? { ...known, name: state.name } : known));
            loadParties();
          }
        }
      } catch { /* hors ligne : on conserve les liens */ }
    }
  }
  refreshParties();
  $("party-chips").addEventListener("click", (e) => {
    const f = e.target.closest("[data-forget]");
    if (f) { recent.forget(f.dataset.forget); loadParties(); return toast("Soirée oubliée sur ce téléphone"); }
    const b = e.target.closest("[data-code]"); if (!b) return;
    if (createMode) $("btn-toggle-create").click();
    $("in-code").value = linkFor(b.dataset.code);
    document.querySelectorAll("#party-chips .chip").forEach((c) => c.classList.toggle("exact", c === b));
    const step = ROLE_STEP[chosenRole];
    if (step?.name && !$("in-name").value) $("in-name").focus();
  });

  $("btn-forget-all").addEventListener("click", () => {
    // « Tout oublier » oublie tout : soirées, prénom, réglages du chalet — pas
    // seulement la liste. Une promesse d'effacement partielle n'en est pas une.
    try {
      Object.keys(localStorage).filter((k) => k.startsWith("veilleuse.")).forEach((k) => localStorage.removeItem(k));
    } catch { /* stockage indisponible */ }
    $("in-name").value = "";
    loadParties(); toast("Tout est effacé sur ce téléphone");
  });

  // Créer ou rejoindre : deux chemins, un seul écran.
  $("btn-toggle-create").addEventListener("click", () => {
    createMode = !createMode;
    $("create-box").classList.toggle("hidden", !createMode);
    $("join-box").classList.toggle("hidden", createMode);
    $("in-code").required = !createMode;
    $("btn-toggle-create").textContent = createMode
      ? "← J'ai un lien, je rejoins une soirée"
      : "Je n'ai pas de lien — créer une soirée";
    $("btn-continue").textContent = createMode ? "Créer la soirée" : ROLE_STEP[chosenRole]?.cta || "Continuer";
    loadParties();
    (createMode ? $("in-partyname") : $("in-code")).focus();
  });

  async function createParty() {
    const name = $("in-partyname").value.trim() || "Soirée";
    let data;
    try {
      const r = await fetch("/api/parties", { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify({ name }) });
      if (!r.ok) throw new Error();
      data = await r.json();
    } catch { return toast("Création impossible, vérifiez la connexion", 4000); }
    recent.add(data.code, data.name);
    pendingCode = data.code; pendingName = data.name;
    $("share-title").textContent = `« ${data.name} » est prête`;
    $("share-link").textContent = linkFor(data.code);
    $("form-home").classList.add("hidden");
    $("share-card").classList.remove("hidden");
  }

  let pendingCode = "", pendingName = "";
  $("btn-share").addEventListener("click", async () => {
    const url = linkFor(pendingCode);
    const text = `Veilleuse pour « ${pendingName} » — le babyphone collectif de la soirée. Ce lien est la clé, gardez-le entre nous : ${url}`;
    try {
      if (navigator.share) return await navigator.share({ title: "Veilleuse", text });
      await navigator.clipboard.writeText(text); toast("Lien copié, collez-le dans votre groupe");
    } catch { toast("Copiez le lien affiché ci-dessus", 4000); }
  });
  $("btn-share-go").addEventListener("click", () => {
    session.code = pendingCode;
    $("share-card").classList.add("hidden"); $("form-home").classList.remove("hidden");
    enterParty();
  });

  $("form-home").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!chosenRole) return toast("Choisissez d'abord ce que fait ce téléphone");
    session.name = $("in-name").value.trim();
    store.set("name", session.name);
    if (createMode) return createParty();
    session.code = codeFromLink($("in-code").value);
    if (!session.code) return toast("Collez le lien reçu par message", 4000);
    enterParty();
  });

  function enterParty() {
    // Mémorisée seulement une fois qu'on y entre vraiment, jamais sur l'écran de la sono
    // qui est souvent un ordinateur partagé.
    if (!isSono) {
      const known = recent.all().find((p) => p.code === session.code);
      recent.add(session.code, known?.name || pendingName || session.code.replace(/-[a-z2-9]{10}$/, ""));
    }
    if (chosenRole === "chalet") startChaletSetup();
    else if (chosenRole === "sono") { location.href = `/?mode=sono#${session.code}`; }
    else startSalle();
  }
  document.querySelectorAll("[data-back]").forEach((b) => b.addEventListener("click", () => { stopEverything(); show("home"); }));

  function stopEverything() {
    detector.stop(); net.close(); salle.stop(); tab.del("resume");
    // tout ce qui tourne s'arrête vraiment : timers du chalet, veille d'écran,
    // compteurs — sinon un changement de rôle traîne des restes de l'ancien.
    clearInterval(chalet.hbTimer); clearInterval(chalet.connTimer);
    chalet.registered = false; chalet.above = 0; chaletLostSince = 0;
    try { wakeLock?.release(); } catch { /* ignore */ } wakeLock = null;
  }

  // Lien périmé, mal recopié, ou soirée supprimée par l'organisateur : on le dit,
  // et on retire la puce locale — elle ne mène plus nulle part.
  function onUnknownParty() {
    if (session.code) recent.forget(session.code);
    stopEverything();
    show("home");
    loadParties();
    toast("Cette soirée n'existe plus. Demandez le lien à l'organisateur.", 7000);
  }

  // ============================================================
  //  ÉMETTEUR (chalet)
  // ============================================================
  const detector = {
    ctx: null, analyser: null, stream: null, raf: null, buf: null, level: 0, onLevel: null, onMicState: null, recorder: null,
    lastSampleAt: 0,
    micAlive() {
      // La piste peut sembler « live » alors que l'analyse ne tourne plus (onglet en
      // arrière-plan, AudioContext suspendu) : on exige aussi un échantillon récent.
      // Sans ça, on enverrait « tout va bien » sans plus rien écouter — un faux silence.
      const t = this.stream?.getAudioTracks()[0];
      return !!t && t.readyState === "live" && !t.muted
        && this.ctx?.state === "running"
        && performance.now() - this.lastSampleAt < 5000;
    },
    async start() {
      if (this.stream) {
        if (this.micAlive()) return true;
        this.stop(); // le flux existe mais le micro est mort : on repart de zéro
      }
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
      } catch (err) { toast("Micro refusé : " + err.message, 4000); return false; }
      // Perte du micro : appel entrant, Siri, verrouillage… (iOS coupe la piste sans prévenir autrement)
      const track = this.stream.getAudioTracks()[0];
      track.addEventListener("ended", () => this.onMicState?.("ended"));
      track.addEventListener("mute", () => this.onMicState?.("muted"));
      track.addEventListener("unmute", () => this.onMicState?.("live"));
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === "suspended") { try { await this.ctx.resume(); } catch { /* tant pis */ } }
      const src = this.ctx.createMediaStreamSource(this.stream);
      this.analyser = this.ctx.createAnalyser(); this.analyser.fftSize = 2048;
      src.connect(this.analyser);
      this.buf = new Float32Array(this.analyser.fftSize);
      const loop = () => {
        this.lastSampleAt = performance.now();
        this.analyser.getFloatTimeDomainData(this.buf);
        let sum = 0; for (let i = 0; i < this.buf.length; i++) sum += this.buf[i] * this.buf[i];
        const rms = Math.sqrt(sum / this.buf.length);
        const db = 20 * Math.log10(rms + 1e-8);            // ~ -80 (silence) .. 0 (saturé)
        const target = Math.max(0, Math.min(100, (db + 65) * (100 / 65)));
        this.level = target > this.level ? target : this.level * 0.85 + target * 0.15; // attaque rapide, retombée douce
        this.onLevel?.(this.level);
        this.raf = requestAnimationFrame(loop);
      };
      loop();
      return true;
    },
    stop() {
      cancelAnimationFrame(this.raf); this.raf = null;
      this.stream?.getTracks().forEach((t) => t.stop()); this.stream = null;
      this.ctx?.close().catch(() => {}); this.ctx = null; this.level = 0;
    },
    // Enregistre quelques secondes et renvoie un data-URL (ou null si trop gros / impossible)
    recordClip(ms = 4000) {
      return new Promise((resolve) => {
        if (!this.stream || typeof MediaRecorder === "undefined") return resolve(null);
        let mime = "";
        try { mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((m) => MediaRecorder.isTypeSupported(m)) || ""; } catch { /* vieux navigateur */ }
        let rec; try { rec = new MediaRecorder(this.stream, mime ? { mimeType: mime, audioBitsPerSecond: 24000 } : { audioBitsPerSecond: 24000 }); } catch { return resolve(null); }
        const chunks = [];
        rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
        rec.onstop = () => {
          const blob = new Blob(chunks, { type: rec.mimeType });
          if (blob.size > 150000) return resolve(null);
          const fr = new FileReader(); fr.onload = () => resolve(fr.result); fr.onerror = () => resolve(null); fr.readAsDataURL(blob);
        };
        rec.start(); setTimeout(() => { try { rec.stop(); } catch { resolve(null); } }, ms);
      });
    },
  };

  const chalet = { id: "", name: "", kids: "", threshold: 55, above: 0, lastNoise: 0, lastAlert: 0, alerting: false, hbTimer: null };

  // La barre montre un niveau absolu : on masque la part non atteinte, le dégradé reste fixe.
  const setLevel = (el, level) => { el.style.width = (100 - Math.max(0, Math.min(100, level))) + "%"; };

  function startChaletSetup() {
    const saved = store.get("chalet", {});
    $("in-chalet").value = saved.name || ""; $("in-kids").value = saved.kids || ""; $("in-threshold").value = saved.threshold || 55;
    chalet.threshold = +$("in-threshold").value;
    $("setup-thr-block").classList.toggle("hidden", !detector.micAlive());
    $("btn-mic").classList.toggle("hidden", detector.micAlive());
    detector.onLevel = (l) => setLevel($("setup-level"), l);
    show("chalet-setup");
  }
  $("in-threshold").addEventListener("input", (e) => { $("run-thr").style.left = e.target.value + "%"; chalet.threshold = +e.target.value; });
  $("btn-mic").addEventListener("click", async () => {
    if (!(await detector.start())) return;
    $("btn-mic").classList.add("hidden");
    $("setup-thr-block").classList.remove("hidden");   // on ne règle qu'une fois le micro vivant
  });

  $("form-chalet").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!(await detector.start())) return;
    chalet.name = $("in-chalet").value.trim(); chalet.kids = $("in-kids").value.trim(); chalet.threshold = +$("in-threshold").value;
    // Identifiant propre à CE téléphone, pas dérivé du nom : deux familles qui
    // écrivent toutes deux « Chalet 4 » ne doivent pas fusionner leurs chalets —
    // l'une croirait sa chambre surveillée par le téléphone de l'autre.
    const saved = store.get("chalet", {});
    chalet.id = saved.deviceId || slug(chalet.name).slice(0, 30) + "-" + Math.random().toString(36).slice(2, 6);
    // ...saved d'abord : le jeton d'émetteur reçu à la première inscription doit
    // survivre aux passages suivants par ce formulaire, sinon le serveur nous
    // prendrait pour un imposteur et ce téléphone repartirait sous une autre tuile.
    store.set("chalet", { ...saved, deviceId: chalet.id, name: chalet.name, kids: chalet.kids, threshold: chalet.threshold });
    startChaletRun();
  });

  function startChaletRun() {
    session.role = "chalet";
    tab.set("resume", { role: "chalet", code: session.code });
    history.replaceState(null, "", "/#" + session.code);
    $("run-chalet").textContent = chalet.name; $("run-kids").textContent = chalet.kids; $("run-thr").style.left = chalet.threshold + "%";
    show("chalet-run");
    keepAwake().then((ok) => {
      const p = $("run-wake");
      p.innerHTML = `<svg class="ic"><use href="#${ok ? "i-sun" : "i-warn"}"/></svg> ${ok ? "écran maintenu" : "écran non maintenu"}`;
      p.className = "pill " + (ok ? "ok" : "warn");
      if (!ok) toast("Ce téléphone ne sait pas garder l'écran allumé : désactivez le verrouillage automatique dans les réglages.", 6000);
    });

    // « Prêt à surveiller » se mérite : rien n'est affirmé avant que le serveur
    // ait accepté l'enregistrement ET que le micro échantillonne réellement.
    chalet.registered = false;
    $("run-status").textContent = "Connexion au serveur…"; $("run-status").className = "run-status";
    $("run-msg").textContent = "La veilleuse s'annonce à la soirée.";
    lastFrame = performance.now(); chalet.above = 0;   // pas de temps fantôme compté comme du bruit

    // Le jeton est PROPRE À LA SOIRÉE : rangé sous une clé globale, celui de la
    // soirée B écrasait celui de A, et revenir à A finissait en register_denied
    // puis en tuile dupliquée silencieuse.
    net.hello = { type: "register", chalet_id: chalet.id, name: chalet.name, kids: chalet.kids,
                  token: store.get("ctok:" + session.code, undefined) || undefined };
    net.onConn = (ok) => { const p = $("run-conn"); p.textContent = ok ? "● connecté" : "○ reconnexion…"; p.className = "pill " + (ok ? "ok" : "bad"); if (ok) sendHeartbeat(); };
    net.onMessage = (m) => {
      if (m.type === "clip_request") return serveClipRequest(m);
      if (m.type === "registered") {
        chalet.registered = true;
        store.set("ctok:" + session.code, m.token);   // le jeton qui prouve « c'est moi », pour CETTE soirée
        net.hello = { ...net.hello, token: m.token };
        $("btn-retake").classList.add("hidden");
        if (detector.micAlive() && !chalet.alerting) setChaletIdleUi();
        sendHeartbeat();   // premier battement dès l'inscription confirmée
        return;
      }
      if (m.type === "superseded") {
        // un autre téléphone (avec le jeton) a repris ce chalet : celui-ci n'émet
        // plus, et le dit — pas de faux « à l'écoute » sur deux écrans à la fois
        chalet.registered = false;
        const st = $("run-status");
        st.textContent = "Repris par un autre téléphone"; st.className = "run-status alert";
        $("run-msg").textContent = "Un autre téléphone s'est enregistré pour ce chalet : celui-ci n'émet plus rien.";
        $("btn-retake").classList.remove("hidden");
        return;
      }
      if (m.type === "register_denied") {
        // le nom est déjà tenu par un autre téléphone (jeton inconnu) : on repart
        // sous un identifiant neuf plutôt que de parler à la place de quelqu'un.
        chalet.id = slug(chalet.name).slice(0, 30) + "-" + Math.random().toString(36).slice(2, 6);
        store.set("chalet", { ...store.get("chalet", {}), deviceId: chalet.id });
        store.set("ctok:" + session.code, undefined);
        net.hello = { type: "register", chalet_id: chalet.id, name: chalet.name, kids: chalet.kids };
        net.send(net.hello, { queueIfOffline: false });
        toast("Ce chalet était déjà tenu par un autre téléphone : celui-ci repart sous une nouvelle tuile.", 7000);
        return;
      }
      if (m.type !== "state") return;
      const me = m.chalets.find((c) => c.id === chalet.id);
      chalet.alerting = !!(me && me.alert);
      chalet.currentAid = me?.alert?.id || "";
      // Celui qui est dans le chalet sait mieux que personne qu'une alerte est fausse
      // (test du clap, parent encore dans la chambre) : il peut l'éteindre d'ici.
      $("btn-cancel").classList.toggle("hidden", !chalet.alerting);
      if (!detector.micAlive()) return; // l'écran « micro coupé » prime sur l'état serveur
      if (!chalet.registered) return;   // pas de « veilleuse allumée » sans enregistrement accepté
      const st = $("run-status");
      if (!me?.alert) { setChaletIdleUi(); }
      else if (me.alert.acked_by) { st.textContent = `${me.alert.acked_by} arrive`; st.className = "run-status alert"; $("run-msg").textContent = "Quelqu'un est en route."; }
      else { st.textContent = "Alerte envoyée"; st.className = "run-status alert"; $("run-msg").textContent = "Les téléphones de la salle sonnent."; }
    };
    net.onUnknown = onUnknownParty;
    net.connect();

    detector.onLevel = onChaletLevel;
    detector.onMicState = onMicState;
    chalet.hbTimer = setInterval(sendHeartbeat, 15000);
    chalet.connTimer = setInterval(chaletConnCheck, 3000);
  }

  // Coupure durable côté chalet : le dire en grand, pas dans une pastille.
  let chaletLostSince = 0;
  function chaletConnCheck() {
    const fresh = net.open && net.lastMsgAt && Date.now() - net.lastMsgAt < 40000;
    if (fresh) {
      if (chaletLostSince && detector.micAlive() && !chalet.alerting) setChaletIdleUi();
      chaletLostSince = 0;
      return;
    }
    if (!chaletLostSince) { chaletLostSince = Date.now(); return; }
    if (Date.now() - chaletLostSince < 15000 || !detector.micAlive()) return;  // l'écran « micro coupé » prime
    const st = $("run-status");
    st.textContent = "Connexion perdue"; st.className = "run-status alert";
    $("run-msg").textContent = "Le chalet apparaît « muet » sur les téléphones de la salle. La détection continue ici et les alertes partiront à la reconnexion.";
  }

  // Micro perdu (appel entrant, verrouillage…) : on prévient ici et on cesse d'envoyer des
  // heartbeats « tout va bien » — le silence est une alerte, la salle verra « chalet muet ».
  function onMicState(state) {
    if (session.role !== "chalet") return;
    if (state === "live") { setChaletIdleUi(); sendHeartbeat(); toast("Micro rétabli"); return; }
    if (state === "ended") detector.stop();
    const st = $("run-status");
    st.textContent = "Micro coupé !"; st.className = "run-status alert";
    $("run-msg").textContent = "La surveillance est interrompue : le chalet va passer « muet » sur les téléphones de la salle. Réactivez le micro.";
    $("btn-remic").classList.remove("hidden");
    if (navigator.vibrate) navigator.vibrate([300, 100, 300]);
  }

  function setChaletIdleUi() {
    const st = $("run-status");
    st.textContent = "À l'écoute"; st.className = "run-status";
    $("run-msg").textContent = "Le micro écoute ; les parents sont prévenus dès qu'un bruit dépasse le seuil.";
    $("btn-remic").classList.add("hidden");
  }

  $("btn-remic").addEventListener("click", async () => {
    if (!(await detector.start())) return;
    detector.onLevel = onChaletLevel;
    setChaletIdleUi(); sendHeartbeat(); toast("Micro rétabli");
  });

  $("btn-retake").addEventListener("click", () => {
    // reprendre la main sur son chalet : ré-inscription avec le jeton — l'autre
    // téléphone sera démis et prévenu à son tour
    if (net.send(net.hello, { queueIfOffline: false })) toast("Reprise demandée…");
    else toast("Hors connexion — réessayez");
  });

  async function sendHeartbeat() {
    if (!chalet.registered) return;   // démis ou pas encore inscrit : on n'émet pas
    if (!detector.micAlive()) return; // micro mort : on se tait, le serveur passera le chalet en « muet »
    let battery = null;
    try { const b = await navigator.getBattery?.(); if (b) battery = Math.round(b.level * 100); } catch { /* iOS */ }
    $("run-batt").innerHTML = battery != null ? `<svg class="ic"><use href="#i-battery"/></svg><span class="num">${battery} %</span>` : "";
    net.send({ type: "hb", chalet_id: chalet.id, level: Math.round(detector.level), battery, threshold: chalet.threshold }, { queueIfOffline: false });
  }

  // Décision d'alerte : niveau au-dessus du seuil pendant ~1,5 s cumulées sur une fenêtre courte
  let lastFrame = performance.now();
  function onChaletLevel(level) {
    setLevel($("run-level"), level);
    const t = performance.now(), dt = t - lastFrame; lastFrame = t;
    // Un trou d'échantillonnage (préparation, onglet suspendu, reprise du micro)
    // n'est pas du bruit : on repart de là sans compter le temps écoulé — sinon la
    // première mesure pouvait à elle seule déclencher l'alerte.
    if (dt > 1000) { chalet.above = 0; return; }
    if (level >= chalet.threshold) chalet.above = Math.min(chalet.above + dt, 4000);
    else chalet.above = Math.max(chalet.above - dt * 0.5, 0);      // un bref creux ne remet pas à zéro
    const nowMs = Date.now();
    if (chalet.above > 300 && nowMs - chalet.lastNoise > 5000) {   // bruit court : information, pas alerte
      chalet.lastNoise = nowMs; net.send({ type: "noise", chalet_id: chalet.id, level: Math.round(level) }, { queueIfOffline: false });
    }
    if (chalet.above >= 1500 && nowMs - chalet.lastAlert > 30000) triggerAlert(Math.round(level));
  }

  async function triggerAlert(level, reason = "noise") {
    chalet.lastAlert = Date.now(); chalet.above = 0;
    // L'occurrence est identifiée ICI, avant l'enregistrement : lire l'identifiant
    // « courant » quatre secondes plus tard pouvait corréler le clip à l'alerte
    // suivante si la nôtre venait d'être réglée.
    const aid = Math.random().toString(36).slice(2, 10);
    net.send({ type: "alert", chalet_id: chalet.id, level, reason, aid });     // d'abord l'alerte, minuscule
    if (navigator.vibrate) navigator.vibrate(50);
    const clip = await detector.recordClip(4000);                             // puis le clip si possible
    if (clip) net.send({ type: "alert_clip", chalet_id: chalet.id, aid, clip }, { queueIfOffline: false });
  }
  // Quelqu'un de la salle demande à entendre ce qui se passe : on enregistre et on renvoie.
  async function serveClipRequest(m) {
    if (!detector.micAlive()) return;
    const who = m.by || "Quelqu'un";
    $("run-listen").innerHTML = `<svg class="ic"><use href="#i-speaker"/></svg> ${esc(who)} écoute…`;
    $("run-listen").classList.remove("hidden");
    const clip = await detector.recordClip(Math.min(Math.max((m.seconds || 10) * 1000, 2000), 15000));
    if (clip) net.send({ type: "clip", chalet_id: chalet.id, clip }, { queueIfOffline: false });
    setTimeout(() => $("run-listen").classList.add("hidden"), 3000);
  }

  // Ce bouton teste la CHAÎNE (serveur → salle → sonneries), pas le micro : seul le
  // clap dans les mains prouve que la détection acoustique et le seuil fonctionnent.
  $("btn-test").addEventListener("click", () => {
    if (!net.send({ type: "test", chalet_id: chalet.id }, { queueIfOffline: false })) return toast("Hors connexion — test impossible");
    toast("Transmission testée : la salle sonne. Pour prouver le micro, tapez dans les mains.", 6000);
  });
  $("btn-cancel").addEventListener("click", () => {
    // liée à l'occurrence en cours : une « fausse alerte » ne peut pas éteindre la suivante.
    // Et « envoyée » seulement si c'est vrai — sendAction vient peut-être de dire le contraire.
    if (sendAction({ type: "resolve", chalet_id: chalet.id, aid: chalet.currentAid || undefined, by: "le chalet" }, "Fausse alerte")) {
      toast("Annulation envoyée — l'écran suivra la confirmation de la salle");
    }
  });
  $("btn-stop").addEventListener("click", () => { stopEverything(); show("home"); });

  // ============================================================
  //  RÉCEPTEUR (salle) & ÉCRAN SONO
  // ============================================================
  const salle = {
    state: null, prev: {}, prevOn: {}, armed: false, audio: null, ownId: "", tickTimer: null, remindTimer: null,
    serverOffset: 0, awaitingClip: null, partyName: "", lastEventTs: null,
    stop() {
      clearInterval(this.tickTimer); clearInterval(this.remindTimer);
      this.state = null; this.prev = {}; this.prevOn = {}; this.lastEventTs = null;
      $("overlay").classList.add("hidden");
    },
  };

  function startSalle() {
    session.role = "salle";
    if (!isSono) tab.set("resume", { role: "salle", code: session.code });
    if (!isSono) history.replaceState(null, "", "/#" + session.code);   // l'URL porte la soirée : notifications et rechargements savent où revenir
    // « Mon chalet » est propre à CHAQUE soirée : celui d'une ancienne fête ne doit
    // pas rendre douces les alertes de la nouvelle.
    salle.ownId = store.get("own:" + session.code, "");
    show("salle");
    const known = recent.all().find((p) => p.code === session.code);
    $("salle-code").textContent = known ? `Soirée « ${known.name} »` : "Soirée";
    $("empty-code").textContent = known?.name || "";
    if (isSono) { $("salle-armed").classList.remove("hidden"); keepAwake(); }
    net.hello = { type: "hello", role: "salle", name: session.name || (isSono ? "écran sono" : "") };
    net.onConn = (ok) => {
      const p = $("salle-conn"); p.textContent = ok ? "● connecté" : "○ reconnexion…"; p.className = "pill " + (ok ? "ok" : "bad");
      if (ok) push.recover();   // le serveur repart parfois de zéro : on lui redonne l'abonnement
    };
    net.onMessage = onSalleMessage;
    net.onUnknown = onUnknownParty;
    net.connect();
    salle.tickTimer = setInterval(renderTiles, 1000);
    salle.remindTimer = setInterval(remind, 8000);
    keepAwake();
  }

  $("btn-arm").addEventListener("click", async () => {
    salle.audio = new (window.AudioContext || window.webkitAudioContext)();
    await salle.audio.resume();
    beep("soft"); navigator.vibrate?.(100);
    let perm = "none";
    try { if ("Notification" in window) perm = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission; } catch { /* ignore */ }
    salle.armed = true; $("salle-armed").classList.add("hidden");
    await push.setup();
    updateSalleStatus();
    // On ne promet que ce qui est prouvé : le push ne sera annoncé opérationnel
    // qu'à la confirmation du serveur, et le bouton d'essai permet de le VOIR.
    if (perm !== "granted") {
      toast("Alertes sonores activées. Notifications refusées : gardez cette page ouverte et l'écran allumé pour être prévenu.", 8000);
    } else if (push.sub) {
      toast("Son activé. Notifications en cours d'activation — quand « Push ✓ » s'affiche, appuyez sur « Tester mes notifications » pour vérifier qu'elles arrivent vraiment.", 8000);
    } else {
      toast("Son activé. Ce navigateur ne permet pas les notifications poussées — gardez la page ouverte. Sur iPhone : Safari → Partager → « Sur l'écran d'accueil », puis rouvrez depuis l'icône.", 9000);
    }
  });
  $("btn-fullscreen").addEventListener("click", () => { (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen?.())?.catch?.(() => {}); });
  $("sel-own").addEventListener("change", (e) => {
    salle.ownId = e.target.value; store.set("own:" + session.code, salle.ownId);
    updateOwnTodo(); renderTiles();
  });

  function onSalleMessage(m) {
    if (m.type === "level") {
      const c = salle.state?.chalets.find((x) => x.id === m.chalet_id); if (c) { c.level = m.level; c.battery = m.battery; c.last_hb = m.ts; }
      const bar = document.querySelector(`.tile[data-id="${m.chalet_id}"] .meter-rest`); if (bar) setLevel(bar, m.level);
      return;
    }
    if (m.type === "clip_ready") {
      if (salle.awaitingClip === m.chalet_id) { salle.awaitingClip = null; playClip(m.chalet_id, "fresh"); }
      return;
    }
    if (m.type === "listen_failed") {
      if (salle.awaitingClip === m.chalet_id) { salle.awaitingClip = null; toast(m.reason || "Écoute impossible", 4000); }
      return;
    }
    if (m.type === "push_ok") { push.confirmed = true; push.status = "ok"; updateSalleStatus(); return; }
    if (m.type === "push_test_sent") return;   // le toast du bouton a déjà prévenu
    if (m.type === "push_test_failed") { toast("Essai impossible : réactivez les alertes pour réabonner ce téléphone.", 5000); return; }
    if (m.type === "action_stale") { toast("Cette alerte n'était plus d'actualité — regardez l'état à jour du chalet.", 5000); return; }
    if (m.type !== "state") return;
    // Le vrai nom vient du serveur : un invité ne l'a pas, il n'a que le lien.
    if (m.name && m.name !== salle.partyName) {
      salle.partyName = m.name;
      $("salle-code").textContent = `Soirée « ${m.name} »`;
      $("empty-code").textContent = m.name;
      if (!isSono) recent.add(session.code, m.name);
    }
    salle.serverOffset = m.now - Date.now() / 1000;
    salle.state = m;
    // Détection des transitions pour sonner/vibrer/notifier. L'overlay, lui, est
    // recalculé globalement : une transition ne peut plus fermer l'urgence d'un
    // autre chalet ni faire disparaître un « chalet muet » à peine affiché.
    for (const c of m.chalets) {
      const before = salle.prev[c.id];
      const isOwn = c.id === salle.ownId;
      const newAlert = (c.status === "alert") && (!before || (before !== "alert" && before !== "acked" && before !== "escalated"));
      const escalated = c.status === "escalated" && before !== "escalated";
      // La perte de surveillance se juge sur la CONNEXION, pas sur le statut : un
      // chalet muet pendant une alerte acquittée gardait le statut « acked » et
      // devenait silencieux sans que personne ne l'entende.
      const wentSilent = salle.prevOn[c.id] === true && !c.online;
      if (newAlert || escalated) notify(c, escalated || isOwn ? "strong" : "soft", escalated ? "Personne n'a répondu" : "Ça sonne");
      else if (wentSilent) notify(c, isOwn ? "strong" : "soft", "Chalet muet");
      salle.prev[c.id] = c.status;
      salle.prevOn[c.id] = c.online;
    }
    for (const id of Object.keys(salle.prev)) if (!m.chalets.find((c) => c.id === id)) { delete salle.prev[id]; delete salle.prevOn[id]; }
    // Renfort, rappel, libération : des ÉVÉNEMENTS, pas des changements de statut —
    // sans ça, une page visible (la sono en tête) restait muette, le push étant
    // par ailleurs avalé quand la soirée est sous les yeux.
    const evs = m.events || [];
    if (salle.lastEventTs === null) {
      salle.lastEventTs = evs.length ? evs[evs.length - 1].ts : 0;   // l'historique d'avant nous ne re-sonne pas
    } else {
      for (const ev of evs) if (ev.ts > salle.lastEventTs) handleLiveEvent(ev);
      if (evs.length) salle.lastEventTs = Math.max(salle.lastEventTs, evs[evs.length - 1].ts);
    }
    renderOwnSelect(); renderTiles(); renderEvents(); updateOverlay();
  }

  function handleLiveEvent(ev) {
    const texts = {
      reinforce: `Renfort demandé — ${ev.chalet_name} (${ev.by || "quelqu'un"})`,
      ack_reminder: `Toujours en cours — ${ev.chalet_name} : ${ev.by || "quelqu'un"} n'a pas confirmé « C'est réglé »`,
      released: `${ev.by || "Quelqu'un"} ne peut plus y aller — ${ev.chalet_name} sonne de nouveau`,
    };
    const text = texts[ev.kind];
    if (!text) return;
    if (salle.armed) { beep("strong"); navigator.vibrate?.([400, 150, 400]); }
    toast(text, 7000);
  }

  // ---------- l'overlay : UNE source de vérité, pas une course de transitions ----------
  // Priorité fixe et lisible : escalade > alerte > chalet muet non pris en charge >
  // quelqu'un y va > vérification en cours. À priorité égale, le plus ancien d'abord :
  // deux urgences ne peuvent plus se remplacer selon l'ordre d'arrivée des chalets.
  function emergencies() {
    const now = Date.now() / 1000 + salle.serverOffset;
    return (salle.state?.chalets || []).map((c) => {
      // La connexion prime sur la prise en charge : « Marie y va » ne prouve pas
      // que le micro fonctionne — un chalet muet reste une urgence même acquitté.
      const silent = !c.online && c.last_hb;
      let prio = 0, ts = now, kind = "";
      if (c.status === "escalated") { prio = 5; ts = c.alert.started; kind = "escalated"; }
      else if (c.status === "alert") { prio = 4; ts = c.alert.started; kind = "alert"; }
      else if (silent && !c.check_by) { prio = 3; ts = c.last_hb; kind = "silent"; }
      else if (c.status === "acked") { prio = 2; ts = c.alert.acked_at; kind = "acked"; }
      else if (silent && c.check_by) { prio = 1; ts = c.check_at || c.last_hb; kind = "checked"; }
      return { c, prio, ts, kind };
    }).filter((e) => e.prio > 0).sort((a, b) => b.prio - a.prio || a.ts - b.ts);
  }
  const emKey = (e) => `${e.c.id}|${e.kind}|${e.c.alert?.id || ""}|${e.c.check_by || ""}`;
  let ovMuted = null;   // ensemble des urgences connues au moment du « Masquer »
  let ovCalmSince = 0;  // début de l'affichage d'un état apaisé (confirmation brève)

  function updateOverlay() {
    const list = emergencies();
    if (!list.length) { $("overlay").classList.add("hidden"); ovMuted = null; return; }
    const top = list[0];
    // « Masquer » ne vaut que pour les urgences CONNUES à ce moment-là : toute
    // urgence nouvelle ou transformée — même moins prioritaire que le sommet —
    // rouvre l'overlay. Un chalet qui devient muet pendant qu'une alerte masquée
    // sonne ailleurs ne doit pas passer sous silence.
    if (ovMuted) {
      const unknown = list.some((e) => !ovMuted.has(emKey(e)));
      if (!unknown) { $("overlay").classList.add("hidden"); return; }
      ovMuted = null;
    }

    const c = top.c;
    const calm = top.kind === "acked" || top.kind === "checked";
    // Un état apaisé (quelqu'un y va, quelqu'un vérifie) n'OUVRE jamais l'overlay,
    // et ne le prolonge que 5 s en guise de confirmation : le tableau doit
    // redevenir visible pendant l'intervention, sur les téléphones comme à la sono.
    if (calm) {
      if ($("overlay").classList.contains("hidden")) return;
      if (!ovCalmSince) ovCalmSince = Date.now();
      if (Date.now() - ovCalmSince > 5000) {
        ovMuted = new Set(list.map(emKey));
        ovCalmSince = 0;
        $("overlay").classList.add("hidden");
        return;
      }
    } else ovCalmSince = 0;
    const kicker = top.kind === "escalated" ? "Personne n'a répondu"
      : top.kind === "alert" ? "Ça sonne"
      : top.kind === "silent" ? "Chalet muet"
      : top.kind === "acked" ? `${c.alert.acked_by} y va`
      : `${c.check_by} va vérifier`;
    $("ov-kicker").textContent = kicker;
    $("ov-name").textContent = c.name; $("ov-name").dataset.id = c.id;
    $("ov-name").dataset.aid = c.alert?.id || ""; $("ov-name").dataset.kind = top.kind;
    $("ov-kids").textContent = c.kids || "";
    const nowS = Date.now() / 1000 + salle.serverOffset;
    $("ov-since").textContent = (top.kind === "silent" || top.kind === "checked")
      ? `plus de nouvelles depuis ${fmtAgo(nowS - c.last_hb)}`
        + (c.alert?.acked_by ? ` · ${c.alert.acked_by} y allait` : "")
      : c.alert
        ? (c.alert.acked_by ? `${c.alert.acked_by} y va depuis ${fmtAgo(nowS - c.alert.acked_at)}`
                            : `sonne depuis ${fmtAgo(nowS - c.alert.started)}`)
        : "";
    // le bouton principal suit l'urgence : y aller, ou aller vérifier
    const act = $("ov-ack");
    if (top.kind === "alert" || top.kind === "escalated") { act.textContent = "J'y vais"; act.classList.remove("hidden"); }
    else if (top.kind === "silent") { act.textContent = "Je vais vérifier"; act.classList.remove("hidden"); }
    else act.classList.add("hidden");
    $("ov-listen").classList.toggle("hidden", !c.online);
    $("ov-more").textContent = list.length > 1 ? `+ ${list.length - 1} autre${list.length > 2 ? "s" : ""} urgence${list.length > 2 ? "s" : ""} — voir le tableau` : "";
    $("ov-more").classList.toggle("hidden", list.length <= 1);
    $("overlay").classList.toggle("acked", calm);
    const wasHidden = $("overlay").classList.contains("hidden");
    $("overlay").classList.remove("hidden");
    if (wasHidden && !isSono) { try { (act.classList.contains("hidden") ? $("ov-dismiss") : act).focus(); } catch { /* rien */ } }
  }

  function notify(c, strength, kicker) {
    if (salle.armed) { beep(strength); navigator.vibrate?.(strength === "strong" ? [400, 150, 400, 150, 800] : [200, 100, 200]); }
    if (!("Notification" in window) || Notification.permission !== "granted" || document.visibilityState === "visible") return;
    const title = `${kicker} — ${c.name}`;
    const opts = { body: c.kids || "", tag: "veilleuse-" + c.id, renotify: true, vibrate: [400, 150, 400, 150, 800],
                   icon: "/static/icon-192.png", badge: "/static/icon-192.png", data: { code: session.code } };
    // Android n'accepte que la voie du service worker : new Notification() y lève
    // « Illegal constructor ». Et jamais d'attente indéfinie sur le service worker.
    swReady(2000)
      .then((reg) => reg?.showNotification ? reg.showNotification(title, opts) : new Notification(title, opts))
      .catch(() => { try { new Notification(title, opts); } catch { /* tant pis */ } });
  }

  // ---------- notifications poussées (Web Push) ----------
  // Le serveur pousse l'alerte jusqu'au téléphone via Google/Apple : ça sonne même
  // app fermée. La page ouverte reste le chemin principal, ceci est le filet.
  // JAMAIS d'attente indéfinie : ready est toujours couru contre un délai —
  // c'est l'attente infinie de ready qui a caché le bug de portée pendant un mois.
  const swReady = (ms = 4000) => Promise.race([
    navigator.serviceWorker.ready,
    new Promise((res) => setTimeout(() => res(null), ms)),
  ]);

  const push = {
    key: null, sub: null,
    confirmed: false,       // le serveur a bien reçu notre abonnement (push_ok)
    status: "off",          // off | unsupported | no-perm | no-sw | failed | pending | ok
    async setup() {
      if (isSono) return;
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) { this.status = "unsupported"; return; }
      if (!("Notification" in window) || Notification.permission !== "granted") { this.status = "no-perm"; return; }
      try {
        if (this.key === null) {
          const r = await fetch("/api/push-key");
          this.key = r.ok ? (await r.json()).key : "";
        }
        if (!this.key) { this.status = "unsupported"; return; }
        let reg = await swReady();
        if (!reg) {   // le SW peut être en cours d'installation : un second essai, puis un verdict
          await new Promise((r) => setTimeout(r, 1500));
          reg = await swReady();
        }
        if (!reg) { this.status = "no-sw"; return; }   // pas d'attente infinie
        try {
          this.sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(this.key) });
        } catch {
          // abonnement existant lié à une ancienne clé : on repart proprement
          const old = await reg.pushManager.getSubscription();
          if (old) await old.unsubscribe();
          this.sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(this.key) });
        }
        this.status = "pending";                        // abonné ≠ confirmé : push_ok tranchera
        this.announce();
      } catch { this.status = "failed"; }
      updateSalleStatus();
    },
    // Le serveur peut avoir tout perdu : on lui redonne l'abonnement à chaque connexion.
    announce() {
      if (this.sub && net.send({ type: "push_sub", sub: this.sub.toJSON() }, { queueIfOffline: false })) {
        if (this.status === "ok") this.status = "pending";   // en attente du push_ok de CE serveur
        this.confirmed = false;
      }
    },
    // Après un rechargement, l'abonnement existe déjà dans le navigateur : on le
    // retrouve sans redemander de geste, pour que le push survive à la reprise.
    async recover() {
      if (this.sub || isSono || !("serviceWorker" in navigator) || !("PushManager" in window)) return this.announce();
      if (!("Notification" in window) || Notification.permission !== "granted") return;
      try {
        const reg = await swReady();
        this.sub = reg ? await reg.pushManager.getSubscription() : null;
        if (this.sub) this.status = "pending";
      } catch { /* pas de push ici */ }
      this.announce();
      updateSalleStatus();
    },
  };
  // Quatre choses distinctes qu'on confondait : le son local, la permission de
  // notifier, l'abonnement push CONFIRMÉ par le serveur, et la connexion. La
  // ligne d'état dit chacune séparément — pas de promesse globale.
  function updateSalleStatus() {
    const el = $("salle-status");
    if (!el || session.role !== "salle" || isSono) return;
    const perm = ("Notification" in window) ? Notification.permission : "unsupported";
    // Chaque coche est une preuve, pas un souvenir : le son peut être suspendu
    // par le navigateur après l'activation, la connexion peut être ouverte mais
    // sans nouvelles fraîches — c'est l'état RÉEL qui s'affiche.
    const sonOk = salle.armed && salle.audio && salle.audio.state === "running";
    const fresh = net.open && net.lastMsgAt && Date.now() - net.lastMsgAt < 40000;
    const parts = [
      sonOk ? "Son ✓" : (salle.armed ? "Son ⚠ (touchez l'écran)" : "Son —"),
      perm === "granted" ? "Notifications ✓" : (perm === "denied" ? "Notifications ✗" : "Notifications —"),
      push.confirmed ? "Push ✓" : (push.status === "pending" ? "Push …" : "Push —"),
      fresh ? "Connexion ✓" : "Connexion ✗",
    ];
    el.textContent = parts.join("  ·  ");
    el.classList.remove("hidden");
    $("btn-push-test").classList.toggle("hidden", !push.confirmed);
  }

  $("btn-push-test").addEventListener("click", () => {
    if (!push.sub) return;
    if (!net.send({ type: "push_test", endpoint: push.sub.endpoint }, { queueIfOffline: false })) {
      return toast("Hors connexion — réessayez");
    }
    toast("Notification d'essai demandée : elle doit apparaître dans quelques secondes, même si vous verrouillez l'écran.", 6000);
  });

  function b64ToBytes(s) {
    const pad = "=".repeat((4 - (s.length % 4)) % 4);
    const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  // Rappel périodique tant qu'une alerte n'est pas acquittée (plus fort si c'est la mienne ou si escalade)
  function remind() {
    if (!salle.state || !salle.armed) return;
    if (connLostSince && Date.now() - connLostSince > 10000) { beep("soft"); return; }
    // un chalet muet que personne n'a pris en charge est une urgence qui attend —
    // y compris pendant une alerte acquittée : le statut ne dit pas la connexion
    const pending = salle.state.chalets.filter((c) => c.status === "alert" || c.status === "escalated"
      || (!c.online && c.last_hb && !c.check_by));
    if (!pending.length) return;
    const strong = pending.some((c) => c.status === "escalated" || c.id === salle.ownId);
    beep(strong ? "strong" : "soft"); navigator.vibrate?.(strong ? [400, 150, 400] : [150]);
  }

  function beep(strength) {
    const ctx = salle.audio; if (!ctx) return;
    const pattern = strength === "strong" ? [[880, 0, .25], [660, .3, .25], [880, .6, .25], [660, .9, .25], [1046, 1.2, .6]] : [[660, 0, .15], [880, .2, .2]];
    for (const [f, at, dur] of pattern) {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "square"; o.frequency.value = f; g.gain.value = strength === "strong" ? .5 : .25;
      o.connect(g).connect(ctx.destination); o.start(ctx.currentTime + at); o.stop(ctx.currentTime + at + dur);
    }
  }

  $("ov-ack").addEventListener("click", () => {
    const d = $("ov-name").dataset;
    if (d.kind === "silent") sendCheck(d.id);
    else ack(d.id, d.aid);
    // pas de fermeture optimiste : l'overlay suivra l'état confirmé par le serveur
  });
  $("ov-dismiss").addEventListener("click", () => {
    const list = emergencies();
    ovMuted = list.length ? new Set(list.map(emKey)) : null;
    $("overlay").classList.add("hidden");   // ré-ouvrira tout seul à la moindre urgence nouvelle
  });
  // « Il pleure vraiment ? » se pose pendant l'alerte, pas après l'avoir masquée.
  $("ov-listen").addEventListener("click", () => requestListen($("ov-name").dataset.id));

  // Les actions humaines ne se mettent jamais en file : envoyées ou pas, on le dit.
  // Une action rejouée après une coupure pouvait toucher l'alerte suivante.
  const sendAction = (msg, label) => {
    if (!net.send(msg, { queueIfOffline: false })) {
      toast(`Hors connexion — « ${label} » non transmis. Réessayez.`, 5000);
      return false;
    }
    return true;
  };
  const ack = (id, aid) => sendAction({ type: "ack", chalet_id: id, aid: aid || undefined, by: session.name }, "J'y vais");
  const resolve = (id, aid) => sendAction({ type: "resolve", chalet_id: id, aid: aid || undefined, by: session.name }, "C'est réglé");
  const sendCheck = (id) => sendAction({ type: "check", chalet_id: id, by: session.name }, "Je vais vérifier");
  const sendRelease = (id, aid) => sendAction({ type: "release", chalet_id: id, aid: aid || undefined, by: session.name }, "Je ne peux plus");
  const sendReinforce = (id, aid) => sendAction({ type: "reinforce", chalet_id: id, aid: aid || undefined, by: session.name }, "Renfort");

  // Sans « mon chalet » choisi, les alertes de sa propre famille restent douces :
  // le rappel reste visible tant que le choix n'est pas fait — et disparaît dès
  // qu'il l'est, sans attendre la prochaine diffusion du serveur.
  function updateOwnTodo() {
    document.querySelector(".own-picker")?.classList.toggle(
      "todo", !salle.ownId && salle.state?.chalets.length > 0);
  }

  function renderOwnSelect() {
    updateOwnTodo();
    const sel = $("sel-own"); const cur = salle.ownId;
    const opts = ['<option value="">— aucun / je veille sur tous —</option>'].concat(salle.state.chalets.map((c) => `<option value="${esc(c.id)}"${c.id === cur ? " selected" : ""}>${esc(c.name)}</option>`));
    if (sel.innerHTML !== opts.join("")) sel.innerHTML = opts.join("");
  }

  // Un état qui a plus de 40 s (ping/pong compris) n'est plus un état, c'est un souvenir.
  let connLostSince = 0;
  function checkFreshness() {
    const fresh = net.open && net.lastMsgAt && Date.now() - net.lastMsgAt < 40000;
    if (fresh) connLostSince = 0;
    else if (!connLostSince) connLostSince = Date.now();
    const lost = connLostSince && Date.now() - connLostSince > 10000;
    $("conn-lost").classList.toggle("hidden", !lost);
    return lost;
  }

  // « Tout va bien » promettait plus que ce qu'on sait : on sait seulement qu'on écoute.
  const STATUS_LABEL = { ok: "À l'écoute", noise: "Un bruit…", alert: "Ça sonne !", escalated: "Personne n'a répondu !", acked: "Quelqu'un y va", offline: "Chalet muet" };
  // ---------- tuiles : mises à jour chirurgicales ----------
  // Reconstruire #tiles en bloc chaque seconde détruisait les boutons sous le
  // doigt : appui et relâchement tombaient sur deux nœuds différents, donc aucun
  // clic n'était émis — « J'y vais » pouvait ne rien faire. Désormais une tuile
  // n'est reconstruite que si sa STRUCTURE change, les durées sont écrites dans
  // des nœuds déjà en place, et rien n'est reconstruit, supprimé ni réordonné
  // pendant qu'un doigt est posé.
  const ic = (n) => `<svg class="ic"><use href="#${n}"/></svg>`;
  let pointerIsDown = false, pointerUpAt = 0;
  const touching = () => pointerIsDown || Date.now() - pointerUpAt < 350;

  function tileLine(c, now) {
    const a = c.alert;
    if (a) {
      let line = a.acked_by ? `${a.acked_by} y va (depuis ${fmtAgo(now - a.acked_at)})` : `sonne depuis ${fmtAgo(now - a.started)}`;
      if (a.reason === "test") line = "test · " + line;
      if (!c.online) line = `⚠ chalet muet pendant l'alerte · ${line}`;   // l'alerte ne masque pas le silence
      return line;
    }
    if (c.status === "offline") {
      const line = c.last_hb ? `plus de nouvelles depuis ${fmtAgo(now - c.last_hb)}` : "jamais connecté";
      return c.check_by ? `${c.check_by} va vérifier · ${line}` : line;
    }
    return "";
  }

  // Tout ce qui change la structure — donc les boutons. Les durées n'y sont pas :
  // c'est ce qui permet de ne rien reconstruire à chaque seconde.
  const tileSig = (c) => JSON.stringify([c.status, c.online, c.name, c.kids, c.threshold, c.battery,
    c.listen_by, c.check_by, c.has_fresh_clip, c.last_hb != null,
    c.alert?.id, c.alert?.acked_by, c.alert?.has_clip, c.alert?.reason,
    c.id === salle.ownId, salle.awaitingClip === c.id]);

  function tileInner(c) {
    const a = c.alert;
    const aid = a ? ` data-aid="${esc(a.id || "")}"` : "";
    // Écouter = demander du frais. Réécouter = rejouer le dernier reçu (aussi le
    // repli quand iOS refuse la lecture automatique faute de geste utilisateur.)
    // Une seule action principale ; le reste en rang serré dessous.
    const secondary = [
      c.status !== "offline"
        ? `<button class="btn ghost" data-listen="${esc(c.id)}">${ic("i-speaker")} ${salle.awaitingClip === c.id ? "…" : "Écouter"}</button>` : "",
      c.has_fresh_clip ? `<button class="btn ghost" data-replay="${esc(c.id)}">${ic("i-replay")} Réécouter</button>` : "",
      a?.has_clip ? `<button class="btn ghost" data-clip="${esc(c.id)}">${ic("i-play")} Écouter l'alerte</button>` : "",
      a ? `<button class="btn ghost" data-resolve="${esc(c.id)}"${aid}>C'est réglé</button>` : "",
      // prise en charge complète : demander de l'aide, ou rendre l'alerte à tous
      c.status === "acked" ? `<button class="btn ghost" data-reinforce="${esc(c.id)}"${aid}>Renfort</button>` : "",
      c.status === "acked" ? `<button class="btn ghost" data-release="${esc(c.id)}"${aid}>Je ne peux plus</button>` : "",
    ].filter(Boolean).join("");
    const primary = a && !a.acked_by
      ? `<button class="btn primary" data-ack="${esc(c.id)}"${aid}>J'y vais</button>`
      : (!c.online && c.last_hb && !c.check_by
        ? `<button class="btn primary" data-check="${esc(c.id)}">Je vais vérifier</button>` : "");
    const actions = (primary || secondary)
      ? `<div class="actions">${primary}${secondary ? `<div class="row">${secondary}</div>` : ""}</div>` : "";
    return `<div class="name">${esc(c.name)}${c.id === salle.ownId ? '<span class="tag">mon chalet</span>' : ""}</div>
      <div class="kids">${esc(c.kids)}</div>
      <div class="meter"><div class="meter-rest"></div><div class="meter-thr" style="left:${c.threshold ?? 55}%"></div></div>
      <div class="status">${STATUS_LABEL[c.status] || c.status}${c.listen_by ? `<span class="listening">${ic("i-speaker")} ${esc(c.listen_by)} écoute</span>` : ""}</div>
      <div class="meta"><span class="line"></span>${c.battery != null ? `<span class="num">${ic("i-battery")} ${c.battery} %</span>` : ""}</div>
      ${actions}`;
  }

  function renderTiles() {
    if (session.role === "salle") checkFreshness();
    if (!salle.state) return;
    const now = Date.now() / 1000 + salle.serverOffset;
    const chalets = [...salle.state.chalets].sort((a, b) => rank(b) - rank(a) || a.name.localeCompare(b.name));
    $("empty").classList.toggle("hidden", chalets.length > 0);
    const box = $("tiles");
    const held = touching();
    const known = new Map([...box.children].map((el) => [el.dataset.id, el]));

    for (const c of chalets) {
      let el = known.get(c.id);
      if (!el) {
        el = document.createElement("div");
        el.dataset.id = c.id; el.dataset.sig = "";
        box.appendChild(el); known.set(c.id, el);
      }
      const sig = tileSig(c);
      if (el.dataset.sig !== sig && !held) {   // structure : jamais pendant un appui
        el.className = "tile" + (c.id === salle.ownId ? " own" : "");
        el.dataset.status = c.status;
        el.innerHTML = tileInner(c);
        el.dataset.sig = sig;
      }
      // volatile : on écrit dans des nœuds existants, aucun bouton n'est détruit
      const line = tileLine(c, now);
      const lineEl = el.querySelector(".meta .line");
      if (lineEl && lineEl.textContent !== line) lineEl.textContent = line;
      const metaEl = el.querySelector(".meta");
      if (metaEl) metaEl.hidden = !line && c.battery == null;
      const rest = el.querySelector(".meter-rest");
      if (rest) setLevel(rest, c.level);
    }

    if (!held) {
      for (const [id, el] of known) if (!chalets.some((c) => c.id === id)) el.remove();
      // réordonner en DÉPLAÇANT les nœuds : un bouton conserve son identité, donc
      // un changement d'ordre ne peut pas transformer un appui en action sur un autre chalet
      chalets.forEach((c, i) => {
        const el = known.get(c.id);
        if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
      });
    }
    updateOverlay();   // durées et priorité rafraîchies au même rythme
    updateSalleStatus();
  }
  const rank = (c) => ({ escalated: 5, alert: 4, acked: 3, offline: 2, noise: 1, ok: 0 }[c.status] ?? 0);
  $("tiles").addEventListener("pointerdown", () => { pointerIsDown = true; }, { passive: true });
  for (const ev of ["pointerup", "pointercancel"]) {
    window.addEventListener(ev, () => { pointerIsDown = false; pointerUpAt = Date.now(); }, { passive: true });
  }
  $("tiles").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.ack) ack(b.dataset.ack, b.dataset.aid);
    if (b.dataset.resolve) resolve(b.dataset.resolve, b.dataset.aid);
    if (b.dataset.check) sendCheck(b.dataset.check);
    if (b.dataset.release) sendRelease(b.dataset.release, b.dataset.aid);
    if (b.dataset.reinforce) sendReinforce(b.dataset.reinforce, b.dataset.aid);
    if (b.dataset.clip) playClip(b.dataset.clip, "alert");
    if (b.dataset.replay) playClip(b.dataset.replay, "fresh");
    if (b.dataset.listen) requestListen(b.dataset.listen);
  });

  // Demande au chalet d'enregistrer ce qui se passe maintenant, et joue le résultat.
  function requestListen(id) {
    if (salle.awaitingClip) return toast("Une écoute est déjà en cours");
    if (!net.send({ type: "listen", chalet_id: id, by: session.name }, { queueIfOffline: false })) {
      return toast("Pas de connexion, réessayez");
    }
    salle.awaitingClip = id;
    toast("Enregistrement en cours au chalet…", 4000);
    renderTiles();
    setTimeout(() => {
      if (salle.awaitingClip === id) { salle.awaitingClip = null; toast("Le chalet n'a pas répondu", 4000); renderTiles(); }
    }, 30000);
  }

  async function playClip(id, kind = "fresh") {
    try {
      const r = await fetch(`/api/party/${encodeURIComponent(session.code)}/chalet/${encodeURIComponent(id)}/clip?kind=${kind}`);
      if (!r.ok) return toast("Pas de clip disponible");
      const { clip } = await r.json(); const p = $("clip-player"); p.src = clip;
      try { await p.play(); } catch { toast("Enregistrement prêt : appuyez sur « Réécouter »", 5000); }
      renderTiles();
    } catch { toast("Lecture impossible"); }
  }

  const EVENT_LABEL = { registered: "a rejoint la soirée", online: "est connecté", offline: "ne donne plus de nouvelles", alert: "sonne", escalated: "sonne sans réponse — escalade", ack: "→ {by} y va", resolved: "réglé par {by}", listen: "→ {by} a écouté", check: "→ {by} va vérifier", released: "→ {by} ne peut plus y aller", reinforce: "→ {by} demande du renfort", ack_reminder: "attend toujours « C'est réglé » ({by})" };
  function renderEvents() {
    const html = [...salle.state.events].reverse().map((e) => `<li data-kind="${esc(e.kind)}"><time>${fmtTime(e.ts)}</time><span><strong>${esc(e.chalet_name || "")}</strong> ${esc((EVENT_LABEL[e.kind] || e.kind).replace("{by}", e.by || ""))}${e.reason === "test" ? " (test)" : ""}</span></li>`).join("");
    $("events").innerHTML = html;
  }

  // ---------- démarrage automatique ----------
  // Recharger la page ne doit pas renvoyer à l'accueil : on reprend le rôle de
  // l'onglet, sauf si un lien vers une AUTRE soirée vient d'être suivi.
  async function resumeChalet(saved) {
    chalet.id = saved.deviceId; chalet.name = saved.name || "Chalet";
    chalet.kids = saved.kids || ""; chalet.threshold = saved.threshold || 55;
    const ok = await detector.start();   // permission déjà accordée : repart sans geste
    startChaletRun();
    if (!ok) onMicState("ended");        // sinon : « Micro coupé », bouton pour le relancer
    else toast("Veilleuse relancée après le rechargement");
  }

  const linkCode = codeFromLink(location.hash);
  const resumed = tab.get("resume", null);
  if (isSono) {
    session.code = linkCode;
    session.name = "écran sono";
    if (session.code) startSalle(); else show("home");
  } else if (resumed?.code && (!linkCode || linkCode === resumed.code)) {
    session.code = resumed.code;
    session.name = store.get("name", "");
    chosenRole = resumed.role;
    if (resumed.role === "chalet") {
      const saved = store.get("chalet", {});
      if (saved.deviceId && saved.name) resumeChalet(saved);
      else { tab.del("resume"); if (linkCode) applyLinkCode(linkCode); }
    } else {
      startSalle();   // la carte « Activer les alertes » réapparaît : le son exige un geste
    }
  } else if (linkCode) {
    applyLinkCode(linkCode);   // arrivée par le lien : il ne reste qu'à choisir son rôle
    // Ouverte par un appui sur une notification : on est un récepteur — le rôle
    // salle est présélectionné, il ne reste que le prénom et « Voir les chalets ».
    if (params.get("src") === "notif") document.querySelector('#form-home [data-role="salle"]')?.click();
  }

  // Taper le lien alors que l'app est déjà ouverte ne change que le fragment : le
  // navigateur ne recharge pas la page, il faut donc écouter le changement nous-mêmes.
  window.addEventListener("hashchange", () => {
    const code = codeFromLink(location.hash);
    if (!code || code === session.code) return;
    if (session.role) { stopEverything(); show("home"); }
    applyLinkCode(code);
    toast("Soirée reconnue depuis le lien — choisissez le rôle de ce téléphone", 5000);
  });

  function applyLinkCode(code) {
    if (createMode) $("btn-toggle-create").click();   // un lien reçu : on rejoint, on ne crée pas
    $("in-code").value = linkFor(code);
    const known = recent.all().find((p) => p.code === code);
    if (known) $("home-role-label").textContent = known.name;
  }

  // Le service worker demande la preuve avant d'avaler une notification : une URL
  // qui contient le code ne dit pas si la page est un récepteur armé et connecté.
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", (e) => {
      if (e.data?.type === "watching?" && e.ports?.[0]) {
        e.ports[0].postMessage({
          watching: session.role === "salle" && session.code === e.data.code
            && salle.armed && net.open && Date.now() - net.lastMsgAt < 40000,
        });
      }
    });
  }

  // Service worker : installation écran d'accueil, cache, et surtout le push.
  // À LA RACINE — enregistré depuis /static/, il ne contrôlait que /static/ :
  // la page « / » restait orpheline, ready ne se résolvait jamais, aucun
  // abonnement push n'a donc jamais abouti. Migration : on désinscrit l'ancien.
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.getRegistrations()
      .then((regs) => Promise.allSettled(regs.filter((r) => r.scope.endsWith("/static/")).map((r) => r.unregister())))
      .catch(() => {})
      .finally(() => navigator.serviceWorker.register("/sw.js").catch(() => {}));
  }
})();
