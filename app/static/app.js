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
    ws: null, queue: [], backoff: 1000, onState: null, onMessage: null, onConn: null, onUnknown: null, open: false, hello: null, timer: null,
    connect() {
      clearTimeout(this.timer);
      const proto = location.protocol === "https:" ? "wss" : "ws";
      let ws;
      try { ws = new WebSocket(`${proto}://${location.host}/ws/${encodeURIComponent(session.code)}`); } catch { return this.retry(); }
      this.ws = ws;
      ws.onopen = () => {
        this.open = true; this.backoff = 1000; this.onConn?.(true);
        if (this.hello) ws.send(JSON.stringify(this.hello));
        // rejoue ce qui n'a pas pu partir (alertes en priorité, on garde l'ordre)
        const q = this.queue.splice(0); q.forEach((m) => ws.send(JSON.stringify(m)));
        if (q.length) toast(`Connexion rétablie, ${q.length} message(s) renvoyé(s)`);
      };
      ws.onmessage = (e) => {
        let m; try { m = JSON.parse(e.data); } catch { return; }
        if (m.type === "unknown_party") return this.onUnknown?.();
        this.onMessage?.(m);
      };
      ws.onclose = () => { this.open = false; this.onConn?.(false); this.retry(); };
      ws.onerror = () => { try { ws.close(); } catch { /* ignore */ } };
    },
    retry() { this.timer = setTimeout(() => this.connect(), this.backoff); this.backoff = Math.min(this.backoff * 1.7, 15000); },
    send(msg, { queueIfOffline = true } = {}) {
      if (this.open && this.ws?.readyState === 1) { this.ws.send(JSON.stringify(msg)); return true; }
      if (queueIfOffline) { this.queue.push(msg); if (this.queue.length > 50) this.queue.splice(0, this.queue.length - 50); }
      return false;
    },
    close() { clearTimeout(this.timer); this.onConn = null; this.onMessage = null; try { this.ws?.close(); } catch { /* ignore */ } this.open = false; },
  };
  setInterval(() => { if (net.open) net.send({ type: "ping" }, { queueIfOffline: false }); }, 25000);

  // ---------- wake lock (garde l'écran allumé) ----------
  let wakeLock = null;
  async function keepAwake() {
    try { wakeLock = await navigator.wakeLock?.request("screen"); return !!wakeLock; } catch { return false; }
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && wakeLock !== null) keepAwake(); });

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

  // Historique local : il ne quitte jamais ce téléphone (localStorage), et il contient
  // la clé de la soirée — donc il s'oublie tout seul, et on peut l'effacer à la main.
  const RECENT_TTL = 7 * 24 * 3600 * 1000;
  const recent = {
    all() {
      const t = Date.now();
      const list = store.get("recent", []).filter((p) => p.code && t - (p.ts || 0) < RECENT_TTL);
      return list;
    },
    add(code, name) {
      const list = recent.all().filter((p) => p.code !== code);
      list.unshift({ code, name: name || code, ts: Date.now() });
      store.set("recent", list.slice(0, 6));
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
    document.querySelectorAll("#form-home [data-role]").forEach((o) => o.classList.toggle("selected", o === b));
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
    $("home-parties").classList.toggle("hidden", createMode || !list.length);
    $("party-chips").innerHTML = list.map((p) =>
      `<span class="chip-wrap"><button type="button" class="btn chip" data-code="${esc(p.code)}">${esc(p.name)}</button>` +
      `<button type="button" class="btn chip-x" data-forget="${esc(p.code)}" aria-label="Oublier ${esc(p.name)}" title="Oublier cette soirée">×</button></span>`).join("");
  }
  $("party-chips").addEventListener("click", (e) => {
    const f = e.target.closest("[data-forget]");
    if (f) { recent.forget(f.dataset.forget); loadParties(); return toast("Soirée oubliée sur ce téléphone"); }
    const b = e.target.closest("[data-code]"); if (!b) return;
    $("in-code").value = linkFor(b.dataset.code);
    document.querySelectorAll("#party-chips .chip").forEach((c) => c.classList.toggle("exact", c === b));
    const step = ROLE_STEP[chosenRole];
    if (step?.name && !$("in-name").value) $("in-name").focus();
  });

  $("btn-forget-all").addEventListener("click", () => {
    recent.clear(); loadParties(); toast("Historique effacé sur ce téléphone");
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

  function stopEverything() { detector.stop(); net.close(); salle.stop(); }

  // Lien périmé ou mal recopié : on le dit, au lieu de laisser tourner un tableau vide.
  function onUnknownParty() {
    stopEverything();
    show("home");
    toast("Cette soirée n'existe pas ou a expiré. Demandez le lien à l'organisateur.", 7000);
  }

  // ============================================================
  //  ÉMETTEUR (chalet)
  // ============================================================
  const detector = {
    ctx: null, analyser: null, stream: null, raf: null, buf: null, level: 0, onLevel: null, onMicState: null, recorder: null,
    micAlive() {
      const t = this.stream?.getAudioTracks()[0];
      return !!t && t.readyState === "live" && !t.muted;
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
    chalet.id = slug(chalet.name);
    store.set("chalet", { name: chalet.name, kids: chalet.kids, threshold: chalet.threshold });
    startChaletRun();
  });

  function startChaletRun() {
    session.role = "chalet";
    $("run-chalet").textContent = chalet.name; $("run-kids").textContent = chalet.kids; $("run-thr").style.left = chalet.threshold + "%";
    show("chalet-run");
    keepAwake().then((ok) => {
      const p = $("run-wake");
      p.innerHTML = `<svg class="ic"><use href="#${ok ? "i-sun" : "i-warn"}"/></svg> ${ok ? "écran maintenu" : "écran non maintenu"}`;
      p.className = "pill " + (ok ? "ok" : "warn");
      if (!ok) toast("Ce téléphone ne sait pas garder l'écran allumé : désactivez le verrouillage automatique dans les réglages.", 6000);
    });

    net.hello = { type: "register", chalet_id: chalet.id, name: chalet.name, kids: chalet.kids };
    net.onConn = (ok) => { const p = $("run-conn"); p.textContent = ok ? "● connecté" : "○ reconnexion…"; p.className = "pill " + (ok ? "ok" : "bad"); if (ok) sendHeartbeat(); };
    net.onMessage = (m) => {
      if (m.type === "clip_request") return serveClipRequest(m);
      if (m.type !== "state") return;
      const me = m.chalets.find((c) => c.id === chalet.id);
      chalet.alerting = !!(me && me.alert);
      // Celui qui est dans le chalet sait mieux que personne qu'une alerte est fausse
      // (test du clap, parent encore dans la chambre) : il peut l'éteindre d'ici.
      $("btn-cancel").classList.toggle("hidden", !chalet.alerting);
      if (!detector.micAlive()) return; // l'écran « micro coupé » prime sur l'état serveur
      const st = $("run-status");
      if (!me?.alert) { st.textContent = "Veilleuse allumée"; st.className = "run-status"; $("run-msg").textContent = "Les parents sont prévenus dès qu'un bruit dépasse le seuil."; }
      else if (me.alert.acked_by) { st.textContent = `${me.alert.acked_by} arrive`; st.className = "run-status alert"; $("run-msg").textContent = "Quelqu'un est en route."; }
      else { st.textContent = "Alerte envoyée"; st.className = "run-status alert"; $("run-msg").textContent = "Les téléphones de la salle sonnent."; }
    };
    net.onUnknown = onUnknownParty;
    net.connect();

    detector.onLevel = onChaletLevel;
    detector.onMicState = onMicState;
    chalet.hbTimer = setInterval(sendHeartbeat, 15000);
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
    st.textContent = "Veilleuse allumée"; st.className = "run-status";
    $("run-msg").textContent = "Les parents sont prévenus dès qu'un bruit dépasse le seuil.";
    $("btn-remic").classList.add("hidden");
  }

  $("btn-remic").addEventListener("click", async () => {
    if (!(await detector.start())) return;
    detector.onLevel = onChaletLevel;
    setChaletIdleUi(); sendHeartbeat(); toast("Micro rétabli");
  });

  async function sendHeartbeat() {
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
    net.send({ type: "alert", chalet_id: chalet.id, level, reason });          // d'abord l'alerte, minuscule
    if (navigator.vibrate) navigator.vibrate(50);
    const clip = await detector.recordClip(4000);                            // puis le clip si possible
    if (clip) net.send({ type: "alert", chalet_id: chalet.id, level, reason, clip }, { queueIfOffline: false });
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

  $("btn-test").addEventListener("click", () => { net.send({ type: "test", chalet_id: chalet.id }); toast("Alerte de test envoyée — « Fausse alerte » pour l'arrêter"); });
  $("btn-cancel").addEventListener("click", () => {
    net.send({ type: "resolve", chalet_id: chalet.id, by: "le chalet" });
    $("btn-cancel").classList.add("hidden");
    toast("Alerte annulée, les téléphones de la salle sont rassurés");
  });
  $("btn-stop").addEventListener("click", () => { clearInterval(chalet.hbTimer); stopEverything(); show("home"); });

  // ============================================================
  //  RÉCEPTEUR (salle) & ÉCRAN SONO
  // ============================================================
  const salle = {
    state: null, prev: {}, armed: false, audio: null, ownId: store.get("own", ""), tickTimer: null, remindTimer: null, serverOffset: 0, awaitingClip: null, partyName: "",
    stop() { clearInterval(this.tickTimer); clearInterval(this.remindTimer); this.state = null; this.prev = {}; $("overlay").classList.add("hidden"); },
  };

  function startSalle() {
    session.role = "salle";
    show("salle");
    const known = recent.all().find((p) => p.code === session.code);
    $("salle-code").textContent = known ? `Soirée « ${known.name} »` : "Soirée";
    $("empty-code").textContent = known?.name || "";
    if (isSono) { $("salle-armed").classList.remove("hidden"); keepAwake(); }
    net.hello = { type: "hello", role: "salle", name: session.name || (isSono ? "écran sono" : "") };
    net.onConn = (ok) => { const p = $("salle-conn"); p.textContent = ok ? "● connecté" : "○ reconnexion…"; p.className = "pill " + (ok ? "ok" : "bad"); };
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
    // Dire tout de suite ce qui marchera : personne ne doit le découvrir à la première alerte.
    toast(perm === "granted"
      ? "Alertes activées : ce téléphone sonnera, vibrera et affichera une notification même écran éteint."
      : "Alertes sonores activées. Sans l'autorisation de notifier, gardez cette page ouverte à l'écran.", 6000);
  });
  $("btn-fullscreen").addEventListener("click", () => { (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen?.())?.catch?.(() => {}); });
  $("sel-own").addEventListener("change", (e) => { salle.ownId = e.target.value; store.set("own", salle.ownId); renderTiles(); });

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
    // Détection des transitions pour notifier
    for (const c of m.chalets) {
      const before = salle.prev[c.id];
      const isOwn = c.id === salle.ownId;
      const newAlert = (c.status === "alert") && (!before || (before !== "alert" && before !== "acked" && before !== "escalated"));
      const escalated = c.status === "escalated" && before !== "escalated";
      const wentOffline = c.status === "offline" && before && before !== "offline";
      if (newAlert || escalated) notify(c, escalated || isOwn ? "strong" : "soft", escalated ? "Personne n'a répondu" : "Ça sonne");
      else if (wentOffline) notify(c, isOwn ? "strong" : "soft", "Chalet muet");
      if (c.status !== "alert" && c.status !== "escalated" && c.status !== "acked" && $("ov-name").dataset.id === c.id) $("overlay").classList.add("hidden");
      salle.prev[c.id] = c.status;
    }
    for (const id of Object.keys(salle.prev)) if (!m.chalets.find((c) => c.id === id)) delete salle.prev[id];
    renderOwnSelect(); renderTiles(); renderEvents();
  }

  function notify(c, strength, kicker) {
    if (salle.armed) { beep(strength); navigator.vibrate?.(strength === "strong" ? [400, 150, 400, 150, 800] : [200, 100, 200]); }
    showOverlay(c, kicker);
    if (!("Notification" in window) || Notification.permission !== "granted" || document.visibilityState === "visible") return;
    const title = `${kicker} — ${c.name}`;
    const opts = { body: c.kids || "", tag: "veilleuse-" + c.id, renotify: true, vibrate: [400, 150, 400, 150, 800],
                   icon: "/static/icon-192.png", badge: "/static/icon-192.png" };
    // Android n'accepte que la voie du service worker : new Notification() y lève
    // « Illegal constructor » — d'où des notifications qui marchaient partout sauf sur téléphone.
    navigator.serviceWorker?.getRegistration()
      .then((reg) => reg?.showNotification ? reg.showNotification(title, opts) : new Notification(title, opts))
      .catch(() => { try { new Notification(title, opts); } catch { /* tant pis */ } });
  }

  // Rappel périodique tant qu'une alerte n'est pas acquittée (plus fort si c'est la mienne ou si escalade)
  function remind() {
    if (!salle.state || !salle.armed) return;
    const pending = salle.state.chalets.filter((c) => c.status === "alert" || c.status === "escalated");
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

  function showOverlay(c, kicker) {
    $("ov-kicker").textContent = kicker; $("ov-name").textContent = c.name; $("ov-name").dataset.id = c.id; $("ov-kids").textContent = c.kids || "";
    $("overlay").classList.remove("hidden"); $("overlay").classList.toggle("acked", c.status === "acked");
    $("ov-listen").classList.toggle("hidden", c.status === "offline");
    renderOverlaySince();
  }

  // Depuis combien de temps ça sonne : c'est cette durée qui décide d'y aller.
  function renderOverlaySince() {
    const id = $("ov-name").dataset.id;
    const c = salle.state?.chalets.find((x) => x.id === id);
    const el = $("ov-since");
    if (!c?.alert) { el.textContent = ""; return; }
    const now = Date.now() / 1000 + salle.serverOffset;
    el.textContent = c.alert.acked_by
      ? `${c.alert.acked_by} y va depuis ${fmtAgo(now - c.alert.acked_at)}`
      : `sonne depuis ${fmtAgo(now - c.alert.started)}`;
  }
  $("ov-ack").addEventListener("click", () => { ack($("ov-name").dataset.id); $("overlay").classList.add("hidden"); });
  $("ov-dismiss").addEventListener("click", () => $("overlay").classList.add("hidden"));
  // « Il pleure vraiment ? » se pose pendant l'alerte, pas après l'avoir masquée.
  $("ov-listen").addEventListener("click", () => requestListen($("ov-name").dataset.id));
  const ack = (id) => net.send({ type: "ack", chalet_id: id, by: session.name });
  const resolve = (id) => net.send({ type: "resolve", chalet_id: id, by: session.name });

  function renderOwnSelect() {
    const sel = $("sel-own"); const cur = salle.ownId;
    const opts = ['<option value="">— aucun / je veille sur tous —</option>'].concat(salle.state.chalets.map((c) => `<option value="${esc(c.id)}"${c.id === cur ? " selected" : ""}>${esc(c.name)}</option>`));
    if (sel.innerHTML !== opts.join("")) sel.innerHTML = opts.join("");
  }

  const STATUS_LABEL = { ok: "Tout va bien", noise: "Un bruit…", alert: "Ça sonne !", escalated: "Personne n'a répondu !", acked: "Quelqu'un y va", offline: "Chalet muet" };
  function renderTiles() {
    if (!salle.state) return;
    const now = Date.now() / 1000 + salle.serverOffset;
    const chalets = [...salle.state.chalets].sort((a, b) => rank(b) - rank(a) || a.name.localeCompare(b.name));
    $("empty").classList.toggle("hidden", chalets.length > 0);
    const html = chalets.map((c) => {
      const a = c.alert;
      let line = "";
      if (a) {
        line = a.acked_by ? `${esc(a.acked_by)} y va (depuis ${fmtAgo(now - a.acked_at)})` : `sonne depuis ${fmtAgo(now - a.started)}`;
        if (a.reason === "test") line = "test · " + line;
      } else if (c.status === "offline") line = c.last_hb ? `plus de nouvelles depuis ${fmtAgo(now - c.last_hb)}` : "jamais connecté";
      // Écouter = demander du frais. Réécouter = rejouer le dernier reçu (aussi le
      // repli quand iOS refuse la lecture automatique faute de geste utilisateur.)
      // Une seule action principale ; le reste en rang serré dessous.
      const ic = (n) => `<svg class="ic"><use href="#${n}"/></svg>`;
      const secondary = [
        c.status !== "offline"
          ? `<button class="btn ghost" data-listen="${esc(c.id)}">${ic("i-speaker")} ${salle.awaitingClip === c.id ? "…" : "Écouter"}</button>` : "",
        c.has_fresh_clip ? `<button class="btn ghost" data-replay="${esc(c.id)}">${ic("i-replay")} Réécouter</button>` : "",
        a?.has_clip ? `<button class="btn ghost" data-clip="${esc(c.id)}" aria-label="Écouter l'alerte">${ic("i-play")}</button>` : "",
        a ? `<button class="btn ghost" data-resolve="${esc(c.id)}">C'est réglé</button>` : "",
      ].filter(Boolean).join("");
      const primary = a && !a.acked_by ? `<button class="btn primary" data-ack="${esc(c.id)}">J'y vais</button>` : "";
      const actions = (primary || secondary)
        ? `<div class="actions">${primary}${secondary ? `<div class="row">${secondary}</div>` : ""}</div>` : "";
      const meta = [line ? `<span>${esc(line)}</span>` : "",
                    c.battery != null ? `<span class="num">${ic("i-battery")} ${c.battery} %</span>` : ""].filter(Boolean).join("");
      return `<div class="tile${c.id === salle.ownId ? " own" : ""}" data-id="${esc(c.id)}" data-status="${c.status}">
        <div class="name">${esc(c.name)}${c.id === salle.ownId ? '<span class="tag">mon chalet</span>' : ""}</div>
        <div class="kids">${esc(c.kids)}</div>
        <div class="meter"><div class="meter-rest" style="width:${100 - c.level}%"></div><div class="meter-thr" style="left:${c.threshold ?? 55}%"></div></div>
        <div class="status">${STATUS_LABEL[c.status] || c.status}${c.listen_by ? `<span class="listening">${ic("i-speaker")} ${esc(c.listen_by)} écoute</span>` : ""}</div>
        <div class="meta">${meta}</div>
        ${actions}</div>`;
    }).join("");
    const box = $("tiles");
    if (box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; }
    // l'overlay suit l'état
    const ovId = $("ov-name").dataset.id; const ovC = chalets.find((c) => c.id === ovId);
    if (ovC && !$("overlay").classList.contains("hidden")) {
      $("overlay").classList.toggle("acked", ovC.status === "acked");
      if (ovC.status === "acked") $("ov-kicker").textContent = `${ovC.alert.acked_by} y va`;
      renderOverlaySince();
    }
  }
  const rank = (c) => ({ escalated: 5, alert: 4, acked: 3, offline: 2, noise: 1, ok: 0 }[c.status] ?? 0);
  $("tiles").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.ack) ack(b.dataset.ack);
    if (b.dataset.resolve) resolve(b.dataset.resolve);
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

  const EVENT_LABEL = { registered: "a rejoint la soirée", online: "est connecté", offline: "ne donne plus de nouvelles", alert: "sonne", escalated: "sonne sans réponse — escalade", ack: "→ {by} y va", resolved: "réglé par {by}", listen: "→ {by} a écouté" };
  function renderEvents() {
    const html = [...salle.state.events].reverse().map((e) => `<li data-kind="${esc(e.kind)}"><time>${fmtTime(e.ts)}</time><span><strong>${esc(e.chalet_name || "")}</strong> ${esc((EVENT_LABEL[e.kind] || e.kind).replace("{by}", e.by || ""))}${e.reason === "test" ? " (test)" : ""}</span></li>`).join("");
    $("events").innerHTML = html;
  }

  // ---------- démarrage automatique ----------
  const linkCode = codeFromLink(location.hash);
  if (isSono) {
    session.code = linkCode;
    session.name = "écran sono";
    if (session.code) startSalle(); else show("home");
  } else if (linkCode) {
    applyLinkCode(linkCode);   // arrivée par le lien : il ne reste qu'à choisir son rôle
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

  // Service worker : permet l'installation sur l'écran d'accueil (icône, plein écran)
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/static/sw.js").catch(() => {});
})();
