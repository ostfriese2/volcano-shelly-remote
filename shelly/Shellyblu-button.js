// IDs deiner beiden BLU Buttons
//Bitte anpassen für Volano reicht ein Button hier BLACK_BUTTON_ID
//Man kann auch ehrere Button verwenden, wie hier der BLUE-Button 
//für andere Sachen, ist aber für den Volcano nicht notwendig.

let BLUE_BUTTON_ID  = 201;  // blauer Button
let BLACK_BUTTON_ID = 200;  // schwarzer Button

let blue1 = 'http://172.16.0.52/relay/0?turn=toggle';
let blue2 = 'http://172.16.0.138/web/powerstate?newstate=0';
let blue3 = '';
let blue4 = 'http://172.16.0.108/script/1/switch_to?toggle';
let black1 = 'http://172.16.0.5:8181/fan/';
let black2 = 'http://172.16.0.5:8181/on';
let black3 = '';
let black4 = 'http://127.0.0.1/relay/0?turn=toggle';

let fan = 'off';
let temp = '190';

// ------------------------------
// Queue / Takt / Timeout (Shelly-kompatibel)
// ------------------------------
let CALL_TICK_MS   = 250;   // alle 250ms max. 1 HTTP-Request starten
let HTTP_TIMEOUT_S = 3;     // kurzer Timeout
let MAX_QUEUE_LEN  = 30;    // Schutz bei hektischem Drücken
let MAX_RETRIES    = 0;     // 0 = keine Retries (meist stabiler)

let _q = [];        // Queue-Array
let _qHead = 0;     // FIFO-Head-Index (ersetzt shift)
let _busy = false;
let _tickHandle = null;

function _queueLen() {
  return _q.length - _qHead;
}

// Kompaktieren: wenn vorne viel "verbraucht" ist, Speicher aufräumen
function _compactQueueIfNeeded() {
  if (_qHead > 20 && _qHead * 2 > _q.length) {
    let nq = [];
    for (let i = _qHead; i < _q.length; i++) {
      nq.push(_q[i]);
    }
    _q = nq;
    _qHead = 0;
  }
}

// Wenn Queue voll: ältesten Eintrag verwerfen (ohne shift)
function _dropOldestIfFull() {
  if (_queueLen() < MAX_QUEUE_LEN) return;
  _qHead++; // "ältesten" überspringen
  _compactQueueIfNeeded();
}

function _enqueue(url) {
  if (!url || url === "") return;

  _dropOldestIfFull();
  _q.push({ url: url, retries: 0 });

  if (_tickHandle === null) {
    _tickHandle = Timer.set(CALL_TICK_MS, true, _processOne);
  }
}

function _dequeue() {
  if (_queueLen() <= 0) return null;
  let job = _q[_qHead];
  _qHead++;
  _compactQueueIfNeeded();
  return job;
}

function _processOne() {
  if (_busy) return;

  let job = _dequeue();
  if (job === null) return;

  _busy = true;

  Shelly.call("HTTP.Request", {
    method: "GET",
    url: job.url,
    timeout: HTTP_TIMEOUT_S
  }, function (res, err) {
    // konservativ: ok nur bei HTTP 2xx und err==0
    let ok = (err === 0 && res && res.code >= 200 && res.code < 300);

    if (!ok) {
      if (job.retries < MAX_RETRIES) {
        job.retries++;
        _enqueue(job.url); // hinten wieder anstellen
      } else {
        // optionales Logging; bei Bedarf auskommentieren
        if (job.url.indexOf('172.16.0.5:8181') >= 0) {
          getURL('http://127.0.0.1/relay/0?turn=on')
        }
        print("HTTP failed:", JSON.stringify({
          url: job.url,
          err: err,
          code: (res ? res.code : null)
        }));
      }
    }

    _busy = false;
  });
}

// Statt direktem HTTP.Request: ab jetzt nur noch enqueuen
function getURL(url) {
  _enqueue(url);
}

function handleBlu(ev) {
  if (!ev || !ev.info) return;

  let info = ev.info;
  let evt  = info.event;

  // Nur BLU-Button-Ereignisse
  if (evt !== "single_push" &&
      evt !== "double_push" &&
      evt !== "triple_push" &&
      evt !== "long_push") {
    return;
  }
  let status = Shelly.getComponentStatus("switch", 0);
  let id = info.id;
  let who = "";

  if (id === BLUE_BUTTON_ID) {
    who = "Blauer Button";
    if (evt === "single_push") getURL(blue1);
    if (evt === "double_push") getURL(blue2);
    if (evt === "long_push")   getURL(blue4);

  } else if (id === BLACK_BUTTON_ID) {
    who = "Schwarzer Button";

    if (evt === "single_push") {
      if (status.output) {
        fan = (fan === 'off') ? 'on' : 'off';
      } else {
        fan = 'off';
      }
      getURL(black1 + fan);
    } 

    if (evt === "double_push") {
      if (!status.output) {
        //temp = '0';
        print(who + " hat " + evt + " gesendet");
        return;
      }
      getURL(black2);
    }

    if (evt === "long_push") {
      if (status.output) {
        fan = 'off';
        temp = '0';
      }
      getURL(black4);
    }

  } else {
    who = "Unbekannter Button";
  }

  print(who + " hat " + evt + " gesendet");
}

Shelly.addEventHandler(handleBlu);
