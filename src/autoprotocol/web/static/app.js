const form = document.querySelector('#upload-form');
const message = document.querySelector('#upload-message');
const stages = {
  queued: 'В очереди. Обработчик запустит запись по очереди.', preparing: 'Подготовка',
  normalizing: 'Подготовка аудио', transcribing: 'Распознавание речи',
  diarizing: 'Разделение говорящих', aligning: 'Сопоставление текста и говорящих',
  transcript_ready: 'Черновой транскрипт готов', failed: 'Обработка прервана'
};
const flags = {
  zero_duration: 'Нет длительности слова', no_speaker_evidence: 'Нет интервала говорящего',
  overlapping_speech: 'Одновременная речь', speaker_boundary: 'Смена говорящего внутри слова',
  insufficient_coverage: 'Недостаточное совпадение времени',
  missing_word_timestamps: 'Нет таймкодов слов', word_text_mismatch: 'Расхождение текста и слов'
};
let selected = location.hash.slice(1);
let rendered = null;
let requestKey = null;
let refreshing = false;
let currentResult = null;
let dirty = false;
let saving = false;
const reviewForm = document.querySelector('#review-form');
const reviewMessage = document.querySelector('#review-message');

async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте заполнение полей.');
    error.status = response.status;
    throw error;
  }
  return data;
}
function node(tag, text, className) {
  const el = document.createElement(tag);
  if (tag === 'button') el.type = 'button';
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function timecode(ms) {
  const seconds = Math.floor(ms / 1000);
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`;
}
form.addEventListener('input', () => { requestKey = null; });
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (saving || (dirty && !confirm('Есть несохранённые изменения. Перейти к новой записи без сохранения?'))) return;
  const file = form.elements.recording.files[0];
  if (!file) return;
  if (file.size > Number(form.dataset.maxBytes)) {
    message.textContent = 'Файл превышает допустимый размер.';
    return;
  }
  const values = new URLSearchParams({title: form.elements.title.value,
    timezone: form.elements.timezone.value, consent: String(form.elements.consent.checked)});
  if (form.elements.occurred_on.value) values.set('occurred_on', form.elements.occurred_on.value);
  requestKey ||= crypto.randomUUID();
  const button = form.querySelector('button');
  button.disabled = true;
  saving = true;
  updateSaveButtons();
  message.textContent = 'Загрузка и проверка записи…';
  try {
    const meeting = await api(`/api/meetings?${values}`, {method: 'POST', body: file,
      headers: {'Content-Type': 'application/octet-stream', 'Idempotency-Key': requestKey}});
    dirty = false;
    saving = false;
    location.hash = meeting.id;
    message.textContent = 'Запись принята. Можно оставаться на странице или вернуться позже.';
    await refresh();
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; saving = false; updateSaveButtons(); }
});
window.addEventListener('hashchange', () => {
  const target = location.hash.slice(1);
  if (target === selected) return;
  if (saving || (dirty && !confirm('Есть несохранённые изменения. Перейти без сохранения?'))) {
    history.replaceState(null, '', `${location.pathname}${location.search}#${selected}`);
    return;
  }
  document.querySelector('#meeting-audio').pause();
  selected = target;
  rendered = null;
  currentResult = null;
  dirty = false;
  document.querySelector('#meeting-result').hidden = true;
  document.querySelector('#revision-history').open = false;
  document.querySelector('#history-list').replaceChildren();
  if (!selected) document.querySelector('#meeting-panel').hidden = true;
  refresh();
});

window.addEventListener('beforeunload', event => {
  if (dirty || saving) { event.preventDefault(); event.returnValue = ''; }
});

function updateSaveButtons() {
  for (const button of document.querySelectorAll('.save-review')) button.disabled = !dirty || saving;
  document.querySelector('#review-fields').disabled = saving;
  document.querySelector('#reload-review').disabled = saving;
}

function checkbox(text, checked, className) {
  const label = node('label', undefined, 'consent');
  const input = node('input');
  input.type = 'checkbox';
  input.checked = checked;
  input.className = className;
  label.append(input, node('span', text));
  return label;
}

function speakerLabel(id, participants) {
  const person = participants.find(p => p.speaker_id === id);
  return person?.display_name ? `${person.display_name} · ${id}` : (id || 'Говорящий неизвестен');
}

function renderResult(result, id) {
  currentResult = result;
  dirty = false;
  const audio = document.querySelector('#meeting-audio');
  if (rendered !== id) audio.src = `/api/meetings/${encodeURIComponent(id)}/audio`;
  const people = document.querySelector('#participants');
  people.replaceChildren();
  for (const person of result.participants) {
    const row = node('div', undefined, 'participant-row');
    row.dataset.speakerId = person.speaker_id;
    const label = node('label', `Имя для ${person.speaker_id}`);
    const input = node('input');
    input.className = 'participant-name';
    input.maxLength = 200;
    input.value = person.display_name || '';
    input.placeholder = 'Имя пока неизвестно';
    label.append(input);
    row.append(label, checkbox('Подтверждаю соответствие имени этому голосу', person.confirmed, 'participant-confirmed'));
    people.append(row);
  }
  if (!result.participants.length) people.append(node('p', 'Модель не выделила голоса. Имена автоматически не назначаются.'));
  const transcript = document.querySelector('#transcript');
  transcript.replaceChildren();
  for (const segment of result.segments) {
    const card = node('article', undefined, 'transcript-segment');
    card.dataset.segmentId = segment.id;
    const seek = node('button', `${timecode(segment.start_ms)} · ${speakerLabel(segment.speaker_id, result.participants)}`);
    seek.addEventListener('click', () => {
      audio.currentTime = segment.start_ms / 1000;
      audio.play().catch(() => { document.querySelector('#meeting-status').textContent = 'Нажмите воспроизведение в плеере.'; });
    });
    const speakerField = node('label', 'Говорящий');
    const select = node('select', undefined, 'segment-speaker');
    select.append(new Option('Говорящий неизвестен', ''));
    for (const person of result.participants) select.append(new Option(speakerLabel(person.speaker_id, result.participants), person.speaker_id));
    select.value = segment.speaker_id || '';
    speakerField.append(select);
    const textField = node('label', 'Текст реплики');
    const text = node('textarea', undefined, 'segment-text');
    text.value = segment.text;
    text.maxLength = 20000;
    text.required = true;
    textField.append(text);
    const original = node('details');
    original.append(node('summary', 'Исходное распознавание'), node('p',
      `${segment.original.speaker_id || 'Говорящий неизвестен'}: ${segment.original.text}`, 'original-text'));
    card.append(seek, speakerField, textField, original);
    if (segment.manual_fields.length) card.append(node('p', 'Есть сохранённые ручные исправления.', 'review-note'));
    if (segment.review_flags.length) card.append(node('p', 'Пометки исходной модели: ' + segment.review_flags.map(f => flags[f] || f).join('; '), 'review-note'));
    card.append(checkbox('Реплика проверена по записи', segment.reviewed, 'segment-reviewed'));
    transcript.append(card);
  }
  document.querySelector('#result-summary').textContent = result.segments.length
    ? `Фрагментов: ${result.segments.length}. Проверено вручную: ${result.review.reviewed_segments}. Спорных без проверки: ${result.review.unreviewed_flagged_segments}.`
    : 'Речь не обнаружена. Проверьте аудиозапись.';
  const notice = document.querySelector('#source-review-notice');
  notice.hidden = !result.review.previous_source_reviews;
  notice.textContent = 'Запись обработана повторно. Правки прежнего результата сохранены в истории, но к новым репликам автоматически не применяются. Проверьте их заново.';
  reviewMessage.textContent = `Сохранённая версия: ${result.review.revision}.`;
  document.querySelector('#meeting-result').hidden = false;
  rendered = id;
  updateSaveButtons();
}

reviewForm.addEventListener('input', event => {
  if (event.target.matches('.participant-name')) {
    event.target.closest('.participant-row').querySelector('.participant-confirmed').checked = false;
  }
  if (event.target.matches('.segment-text, .segment-speaker')) {
    event.target.closest('.transcript-segment').querySelector('.segment-reviewed').checked = false;
  }
  dirty = true;
  reviewMessage.textContent = 'Есть несохранённые изменения.';
  updateSaveButtons();
});

function collectPatch() {
  const participants = [];
  const segments = [];
  for (const row of document.querySelectorAll('.participant-row')) {
    const speaker_id = row.dataset.speakerId;
    const display_name = row.querySelector('.participant-name').value.trim() || null;
    const confirmed = row.querySelector('.participant-confirmed').checked;
    if (Boolean(display_name) !== confirmed) throw new Error(`Для ${speaker_id} укажите имя и подтвердите его либо оставьте имя и подтверждение пустыми.`);
    const old = currentResult.participants.find(p => p.speaker_id === speaker_id);
    if (display_name !== old.display_name || confirmed !== old.confirmed) participants.push({speaker_id, display_name, confirmed});
  }
  for (const card of document.querySelectorAll('.transcript-segment')) {
    const id = card.dataset.segmentId;
    const text = card.querySelector('.segment-text').value;
    const speaker_id = card.querySelector('.segment-speaker').value || null;
    const reviewed = card.querySelector('.segment-reviewed').checked;
    if (!text.trim()) throw new Error('Текст реплики не может быть пустым.');
    const old = currentResult.segments.find(s => s.id === id);
    if (text !== old.text || speaker_id !== old.speaker_id || reviewed !== old.reviewed) segments.push({id, text, speaker_id, reviewed});
  }
  return {source_digest: currentResult.review.source_digest,
    expected_revision: currentResult.review.revision, participants, segments};
}

reviewForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (saving || !currentResult) return;
  const id = selected;
  try {
    const patch = collectPatch();
    saving = true;
    updateSaveButtons();
    reviewMessage.textContent = 'Сохранение…';
    const result = await api(`/api/meetings/${encodeURIComponent(id)}/result`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(patch)
    });
    if (selected !== id) return;
    renderResult(result, id);
    reviewMessage.textContent = `Изменения сохранены. Версия ${result.review.revision}.`;
    if (document.querySelector('#revision-history').open) await loadHistory();
  } catch (error) {
    reviewMessage.textContent = error.status === 409
      ? `${error.message} Ваш текст остаётся в полях. Скопируйте нужные правки перед загрузкой сохранённой версии.`
      : error.message;
  } finally { saving = false; updateSaveButtons(); }
});

document.querySelector('#reload-review').addEventListener('click', async () => {
  if (saving || (dirty && !confirm('Заменить несохранённые изменения последней сохранённой версией?'))) return;
  const id = selected;
  try {
    // Freeze fields while fetching; otherwise a late response could erase a new edit.
    saving = true;
    updateSaveButtons();
    const result = await api(`/api/meetings/${encodeURIComponent(id)}/result`);
    if (selected === id) {
      renderResult(result, id);
      if (document.querySelector('#revision-history').open) await loadHistory();
    }
  } catch (error) { reviewMessage.textContent = error.message; }
  finally { saving = false; updateSaveButtons(); }
});

async function loadHistory() {
  const id = selected;
  const list = document.querySelector('#history-list');
  list.replaceChildren(node('p', 'Загрузка истории…'));
  try {
    const revisions = await api(`/api/meetings/${encodeURIComponent(id)}/revisions`);
    if (selected !== id) return;
    list.replaceChildren();
    if (!revisions.length) list.append(node('p', 'Ручных изменений пока нет.'));
    const fieldNames = {display_name: 'Имя', text: 'Текст', speaker_id: 'Говорящий', reviewed: 'Проверено'};
    const value = v => v === null ? 'не указано' : (v === true ? 'да' : (v === false ? 'нет' : v));
    for (const revision of revisions) {
      const entry = node('article', undefined, 'history-entry');
      const previous = revision.source_digest !== currentResult.review.source_digest;
      entry.append(node('p', `Версия ${revision.revision} · ${new Date(revision.changed_at).toLocaleString()}${previous ? ' · предыдущий результат обработки' : ''}`));
      for (const change of revision.changes) entry.append(node('p',
        `${change.id} · ${fieldNames[change.field] || change.field}\nБыло: ${value(change.old)}\nСтало: ${value(change.new)}`, 'history-change'));
      list.append(entry);
    }
  } catch (error) { if (selected === id) list.replaceChildren(node('p', error.message)); }
}
document.querySelector('#revision-history').addEventListener('toggle', event => {
  if (event.target.open) loadHistory();
});

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const meetings = await api('/api/meetings');
    const list = document.querySelector('#meeting-list');
    list.replaceChildren();
    document.querySelector('#list-message').textContent = meetings.length ? '' : 'Встреч пока нет.';
    for (const meeting of meetings) {
      const label = meeting.status === 'failed' ? 'Ошибка обработки' : (stages[meeting.stage] || meeting.stage);
      const button = node('button', `${meeting.title} — ${label}`, 'meeting-link');
      button.addEventListener('click', () => { location.hash = meeting.id; });
      list.append(button);
    }
    if (!selected) return;
    const id = selected;
    const meeting = await api(`/api/meetings/${encodeURIComponent(id)}`);
    if (selected !== id) return;
    document.querySelector('#meeting-panel').hidden = false;
    document.querySelector('#meeting-title').textContent = meeting.title;
    document.querySelector('#meeting-status').textContent = meeting.status === 'failed'
      ? `${meeting.error} Этап: ${stages[meeting.stage] || meeting.stage}.`
      : (stages[meeting.stage] || meeting.stage);
    if (rendered !== id) document.querySelector('#meeting-result').hidden = true;
    if (meeting.status !== 'ready' || rendered === id) return;
    const result = await api(`/api/meetings/${encodeURIComponent(id)}/result`);
    if (selected !== id) return;
    renderResult(result, id);
  } catch (error) { document.querySelector('#list-message').textContent = error.message; }
  finally { refreshing = false; }
}
refresh();
setInterval(refresh, 3000);
