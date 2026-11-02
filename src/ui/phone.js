'use strict';
const $ = id => document.getElementById(id);
const api = () => window.pywebview.api;
let modalId = null, refreshing = false, sending = false;
function expandSection(name) {
  const target={setup:'setup-tools',diagnostics:'diagnostics',about:'about-panel',keys:'old-keys',license:'license-details'}[name];
  if(!target)return;
  const section=$(target);
  for(let current=section;current;current=current.parentElement?.closest('details'))current.open=true;
  section.querySelector('summary').focus({preventScroll:true});
  section.scrollIntoView({block:'start',behavior:'instant'});
}

function setText(id, text) {
  if ($(id).textContent !== text) $(id).textContent = text;
}

function renderDiagnostics(d={}, state='stopped') {
  const fresh = d.fresh === true && state === 'connected';
  const connected = state === 'connected';
  const count = n => Math.max(0, Number.isFinite(n) ? Math.floor(n) : 0).toLocaleString();
  for (const [id,key] of [['waiting-value','waiting'],['rejected-value','rejected'],['failed-value','failures'],['peak-active','peak_active'],['peak-waiting','peak_waiting'],['dns-freed','dns_parked']]) setText(id,count(d[key]));
  setText('diagnostic-live',fresh?'Live':connected?'Delayed':'Inactive');
  $('diagnostic-live').className='status-pill '+(fresh?'good':connected?'warning':'');
  setText('diagnostic-context',fresh?'Live queue counts and totals for this session.':connected?'Status is delayed. These counts may be out of date.':d.recorded?'Last session totals. Health checks are inactive.':'Counters appear when a connection starts.');
  for(const key of ['usb','internet']) {
    const p=d[key]||{};
    const condition = !connected ? 'inactive' : !fresh ? 'unknown' : p.condition || 'pending';
    const label={inactive:'Not checked',unknown:'Unknown',pending:'Checking…',warning:'Needs attention',good:key==='usb'?'Responding':'HTTPS passed'}[condition]||'Unknown';
    setText(key+'-state',!connected&&d.recorded?'Inactive':label);
    $(key+'-state').className=condition==='good'?'good':condition==='warning'?'warning':'';
    let detail = connected ? 'Waiting for a fresh observation' : key==='usb'?'Available when connected':'HTTPS check through your phone';
    if(fresh && Number.isFinite(p.age)) detail='Last passed '+Math.round(p.age)+'s ago'+(key==='usb'&&Number.isFinite(p.latency)?' · '+Math.round(p.latency)+' ms':'');
    if(fresh && p.failures) detail+=' · '+count(p.failures)+' recent check failure'+(p.failures===1?'':'s');
    setText(key+'-detail',detail);
  }
  const pressure=fresh&&(d.waiting>0||(d.capacity>0&&d.active>=d.capacity*.85));
  setText('capacity-state',!connected?'Not in use':!fresh?'Unknown':d.waiting>0?'Requests waiting':pressure?'High usage':'Available');
  $('capacity-state').className=pressure?'warning':fresh?'good':'';
  setText('capacity-detail',fresh?count(d.active)+' active · '+count(d.waiting)+' waiting':connected?'Waiting for current counters':'Up to 384 sessions');
}

function render(state) {
  for (const name of ['status', 'message', 'download', 'upload', 'sessions', 'health_label']) {
    if (state[name]?.text !== undefined) setText(name,name==='health_label'?state[name].text.split('\n').filter(line=>!line.startsWith('Peaks:')).join('\n'):state[name].text);
  }
  $('status').style.color = state.status?.fg || '';
  const connectionState=state.connection_state || 'stopped';
  $('state-dot').className='state-dot '+(['connected','ready'].includes(connectionState)?'good':['error','cleanup-needed'].includes(connectionState)?'error':['checking','preparing','verifying','disconnecting'].includes(connectionState)?'warning':'');
  renderDiagnostics(state.diagnostics,connectionState);
  const button = state.power_button || {};
  $('power-text').textContent = (button.text || 'Starting…').replace(/^⏻\s*/, '');
  $('power').disabled = sending || button.state === 'disabled';
  $('power').classList.toggle('connected', button.style === 'Stop.TButton');
  const app=state.app||{};
  document.querySelectorAll('.app-version').forEach(el=>{el.textContent=app.version||'1.2.0';});
  $('pair_phone').disabled=sending||!app.can_pair;
  $('pair_phone').hidden=app.paired===true;
  setText('pairing-status',app.paired?'Paired':app.paired===false?'Not paired':'Checking pairing…');
  setText('pairing-note',app.paired?'Your existing pairing is kept. Use the receiver below only when installing or updating the phone relay.':'A private pairing key stays encrypted for this Windows account.');
  if(app.paired===false && connectionState==='stopped') setText('power-text','Set up phone');
  $('power').dataset.needsPairing=String(app.paired===false);
  const maintenance = state.maintenance || {};
  for (const [id, flag] of [['prepare_update','can_prepare'],['send_update','can_send'],['copy_receiver','can_copy'],['retire_keys','can_retire']]) $(id).disabled = sending || !maintenance[flag] || (id==='prepare_update' && app.paired===false);
  $('receiver-card').hidden = !maintenance.receiver;
  if ($('receiver-command').value !== (maintenance.receiver || '')) $('receiver-command').value = maintenance.receiver || '';
  for (const key of ['update','keys']) {
    const message = maintenance[key+'_result'] || '';
    setText(key+'-result',message);
    $(key+'-result').hidden = !message;
  }
  setText('keys-prerequisite', maintenance.can_retire ? 'Phone check passed. Retirement is available.' : 'Disconnect, then use Check phone above first.');
  const notice = state.notice;
  $('notice').hidden = !notice;
  if (notice) { setText('notice-title',notice.title); setText('notice-message',notice.message); }
  const dialog = state.dialog;
  if (dialog && modalId !== dialog.id) {
    modalId = dialog.id;
    $('dialog-title').textContent = dialog.title;
    $('dialog-message').textContent = dialog.message;
    $('confirm').hidden = !dialog.confirm;
    $('cancel').textContent = dialog.confirm ? 'Cancel' : 'Close';
    $('dialog').showModal();
    $('cancel').focus();
  } else if (!dialog && modalId) {
    modalId = null;
    $('dialog').close();
  }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try { render(await api().get_state()); }
  catch (_) {
    for (const id of ['power','prepare_update','send_update','copy_receiver','retire_keys','pair_phone']) $(id).disabled = true;
    $('health_label').textContent = 'App status is unavailable. Connection health is unknown.';
    renderDiagnostics({},'connected');
    $('state-dot').className='state-dot warning';
  }
  finally { refreshing = false; }
}

async function command(name) {
  if (sending) return;
  sending = true;
  if (name === 'power') $('power').disabled = true;
  try { await api().command(name); }
  catch (_) { $('health_label').textContent = 'The action was not confirmed. Check the app status before trying again.'; }
  finally { sending = false; setTimeout(refresh, 150); }
}

async function answer(accepted) {
  if (!modalId) return;
  $('confirm').disabled = $('cancel').disabled = true;
  try { await api().answer_dialog(modalId, accepted); }
  catch (_) { $('health_label').textContent = 'Your choice was not confirmed. Try again when the app responds.'; }
  finally { $('confirm').disabled = $('cancel').disabled = false; }
}

// PC Center's pointer-captured title-bar movement, with the same native helper.
let titlebarDrag = null;
function beginTitlebarDrag(event) {
  if (event.button !== 0 || event.target.closest('button, a, input, select')) return;
  titlebarDrag = {pointerId:event.pointerId, offsetX:event.clientX, offsetY:event.clientY, point:null, frame:0};
  event.currentTarget.setPointerCapture(event.pointerId);
  event.preventDefault();
}
function moveTitlebarDrag(event) {
  const drag = titlebarDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  drag.point = {x:event.screenX-drag.offsetX, y:event.screenY-drag.offsetY};
  if (drag.frame) return;
  drag.frame = requestAnimationFrame(() => {
    if (titlebarDrag !== drag) return;
    drag.frame = 0;
    const point = drag.point; drag.point = null;
    if (point) api().move_window(point.x, point.y);
  });
}
function endTitlebarDrag(event) {
  const drag = titlebarDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  if (drag.frame) cancelAnimationFrame(drag.frame);
  if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  titlebarDrag = null;
}
window.addEventListener('pywebviewready', () => {
  for (const name of ['power','copy','prepare_update','send_update','copy_receiver','retire_keys','close','dismiss_notice','pair_phone']) {
    $(name).addEventListener('click', () => name==='power' && $('power').dataset.needsPairing==='true' ? expandSection('setup') : command(name));
  }
  for (const button of document.querySelectorAll('[data-expand]')) button.addEventListener('click',()=>expandSection(button.dataset.expand));
  $('copy-update-command').addEventListener('click',()=>command('copy'));
  $('minimize').addEventListener('click', () => api().minimize());
  $('confirm').addEventListener('click', () => answer(true));
  $('cancel').addEventListener('click', () => answer(false));
  $('dialog').addEventListener('cancel', event => { event.preventDefault(); answer(false); });
  for (const [event, handler] of [['pointerdown',beginTitlebarDrag], ['pointermove',moveTitlebarDrag], ['pointerup',endTitlebarDrag], ['pointercancel',endTitlebarDrag]]) $('titlebar').addEventListener(event,handler);
  refresh(); setInterval(refresh,350);
});
document.addEventListener('click', event => { if (event.target.closest('a')) event.preventDefault(); });

