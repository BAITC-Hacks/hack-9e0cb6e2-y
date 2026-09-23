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
let analysisKey = null;
let analyzing = false;
let analysisPending = false;
let currentAnalysis = null;
let analysisDirty = false;
let analysisSaving = false;
let analysisStale = false;
let analysisReadSequence = 0;
const analysisForm = document.querySelector('#analysis-review-form');
const analysisReviewMessage = document.querySelector('#analysis-review-message');
let exporting = false;
let exportMeeting = null;

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
  if (saving || analysisSaving || ((dirty || analysisDirty) && !confirm('Есть несохранённые изменения. Перейти к новой записи без сохранения?'))) return;
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
    analysisDirty = false;
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
  if (saving || analysisSaving || ((dirty || analysisDirty) && !confirm('Есть несохранённые изменения. Перейти без сохранения?'))) {
    history.replaceState(null, '', `${location.pathname}${location.search}#${selected}`);
    return;
  }
  document.querySelector('#meeting-audio').pause();
  selected = target;
  rendered = null;
  currentResult = null;
  analysisKey = null;
  analyzing = false;
  analysisPending = false;
  currentAnalysis = null;
  analysisDirty = false;
  analysisStale = false;
  analysisReadSequence++;
  analysisForm.hidden = true;
  analysisReviewMessage.textContent = '';
  document.querySelector('#analysis-history').hidden = true;
  document.querySelector('#analysis-history').open = false;
  document.querySelector('#analysis-result').replaceChildren();
  document.querySelector('#analysis-message').textContent = '';
  document.querySelector('#analysis-request-error').textContent = '';
  dirty = false;
  document.querySelector('#meeting-result').hidden = true;
  document.querySelector('#revision-history').open = false;
  document.querySelector('#history-list').replaceChildren();
  if (!selected) document.querySelector('#meeting-panel').hidden = true;
  refresh();
});

window.addEventListener('beforeunload', event => {
  if (dirty || saving || analysisDirty || analysisSaving) { event.preventDefault(); event.returnValue = ''; }
});

function updateSaveButtons() {
  for (const button of document.querySelectorAll('.save-review')) button.disabled = !dirty || saving || analysisSaving || analysisDirty;
  document.querySelector('#review-fields').disabled = saving || analysisSaving || analysisDirty;
  document.querySelector('#reload-review').disabled = saving || analysisSaving || analysisDirty;
  document.querySelector('#analyze-meeting').disabled = dirty || saving || analyzing || analysisPending || analysisDirty || analysisSaving;
  document.querySelector('#save-analysis').disabled = !analysisDirty || analysisSaving || saving || dirty || analysisStale;
  document.querySelector('#reload-analysis').disabled = analysisSaving || saving;
  document.querySelector('#analysis-review-fields').disabled = analysisSaving || saving || dirty || analysisStale;
  updateExportButton();
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
  configureExport(id);
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
  if (saving || analysisSaving || analysisDirty || !currentResult) return;
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
  if (saving || analysisSaving || analysisDirty || (dirty && !confirm('Заменить несохранённые изменения последней сохранённой версией?'))) return;
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

document.querySelector('#analyze-meeting').addEventListener('click', async () => {
  if (!currentResult || dirty || saving || analyzing || analysisPending || analysisDirty || analysisSaving) return;
  const id = selected;
  analysisPending = true;
  document.querySelector('#analysis-request-error').textContent = '';
  updateSaveButtons();
  try {
    await api(`/api/meetings/${encodeURIComponent(id)}/analysis`, {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
        expected_revision: currentResult.review.revision, source_digest: currentResult.review.source_digest
      })});
    if (selected === id) await refreshAnalysis(id);
  } catch (error) {
    if (selected === id) document.querySelector('#analysis-request-error').textContent = error.message;
  } finally { if (selected === id) { analysisPending = false; updateSaveButtons(); } }
});

async function refreshAnalysis(id) {
  const sequence = ++analysisReadSequence;
  const data = await api(`/api/meetings/${encodeURIComponent(id)}/analysis`);
  if (selected !== id || sequence !== analysisReadSequence || analysisSaving) return;
  analyzing = ['queued', 'processing'].includes(data.status);
  const labels = {not_started: 'Анализ ещё не запускался.', queued: 'Анализ в очереди.',
    processing: 'Модель анализирует запись… Это может занять несколько минут.',
    ready: 'Черновик готов. Проверьте поручения и итоги по источникам.', failed: data.error};
  // Warn also when this tab still displays an older saved transcript revision.
  const visibleMismatch = data.result && (data.source_digest !== currentResult?.review.source_digest ||
    data.revision !== currentResult?.review.revision);
  const stale = data.stale || visibleMismatch;
  analysisStale = stale || (analysisDirty && currentAnalysis?.id !== data.id);
  updateSaveButtons();
  document.querySelector('#analysis-message').textContent = (stale
    ? 'Версия анализа отличается от транскрипта. Загрузите сохранённый транскрипт и запустите анализ заново. ' : '') +
    (dirty ? 'Есть несохранённые правки; этот анализ относится к сохранённому тексту. ' : '') + labels[data.status] +
    (data.upgrade_available ? ' Доступен новый анализ с таблицей показателей: нажмите «Составить поручения и саммари». Старые правки сохранятся в истории прежнего анализа.' : '');
  if (analysisDirty) {
    if (data.id !== currentAnalysis?.id || data.result?.review.revision !== currentAnalysis?.result.review.revision) {
      analysisReviewMessage.textContent = 'Сохранённые поручения изменились. Ваши правки остаются в полях; скопируйте их перед загрузкой сохранённой версии.';
    }
    return;
  }
  const key = JSON.stringify([id, data.id, data.status, stale, data.result?.review.revision]);
  if (analysisKey === key) return;
  analysisKey = key;
  renderAnalysis(data, stale);
}

function renderAnalysis(data, stale) {
  currentAnalysis = data;
  analysisStale = stale;
  analysisDirty = false;
  analysisForm.hidden = !data.result;
  document.querySelector('#analysis-history').hidden = !data.result;
  analysisReviewMessage.textContent = data.result ? `Сохранённая версия поручений: ${data.result.review.revision}.` : '';
  const panel = document.querySelector('#analysis-result');
  panel.replaceChildren();
  panel.className = stale ? 'stale-analysis' : '';
  updateSaveButtons();
  if (!data.result) return;
  function sources(item) {
    const details = node('details');
    details.append(node('summary', 'Источники в записи'));
    for (const source of item.evidence) {
      const seek = node('button', `${timecode(source.start_ms)} · ${source.speaker_id || 'Говорящий неизвестен'}`);
      seek.addEventListener('click', () => {
        const audio = document.querySelector('#meeting-audio');
        audio.currentTime = source.start_ms / 1000;
        audio.play().catch(() => { document.querySelector('#analysis-message').textContent = 'Нажмите воспроизведение в плеере.'; });
        const card = [...document.querySelectorAll('.transcript-segment')].find(el => el.dataset.segmentId === source.segment_id);
        // Old segment IDs may have been reassigned after reprocessing.
        if (!stale && card) card.scrollIntoView({block: 'center', behavior: 'smooth'});
      });
      details.append(seek, node('p', source.quote, 'original-text'));
    }
    return details;
  }
  const statuses = {agreed: 'Поручено / согласовано', proposed: 'Предложено', changed: 'Изменено', cancelled: 'Отменено'};
  function field(card, title, name, value, options, maxLength) {
    const label = node('label', title);
    const input = node(options ? 'select' : (name === 'task' || name === 'text' ? 'textarea' : 'input'));
    input.dataset.field = name;
    if (options) for (const [key, text] of Object.entries(options)) input.append(new Option(text, key));
    else input.maxLength = maxLength;
    input.value = value || '';
    input.required = name === 'task' || name === 'text';
    label.append(input);
    card.append(label);
  }
  function reviewControls(card, item) {
    card.append(checkbox('Проверено по записи', item.reviewed, 'item-reviewed'),
      checkbox('Исключить из протокола', item.excluded, 'item-excluded'));
    const original = node('details');
    original.append(node('summary', 'Исходный ответ модели'),
      node('p', Object.entries(item.original).map(([key, value]) => `${analysisFieldNames[key] || key}: ${value ?? 'не указано'}`).join('\n'), 'original-text'));
    if (item.manual_fields.length) card.append(node('p', 'Есть сохранённые ручные исправления.', 'review-note'));
    card.append(original, sources(item));
  }
  panel.append(node('h4', 'Поручения'));
  if (!data.result.actions.length) panel.append(node('p', 'Модель не выделила поручений. Проверьте запись: это не гарантия их отсутствия.'));
  for (const action of data.result.actions) {
    const card = node('article', undefined, 'action-card');
    card.dataset.actionId = action.id;
    const due = action.due_normalized;
    const normalized = due.date || (due.interval_start ? `${due.interval_start} — ${due.interval_end}` : '');
    field(card, 'Поручение', 'task', action.task, null, 1000);
    field(card, 'Ответственный (пусто — требует уточнения)', 'assignee_text', action.assignee_text, null, 200);
    field(card, 'Срок: исходная формулировка', 'due_text', action.due_text, null, 300);
    field(card, 'Тип срока', 'due_kind', action.due_kind, {unspecified: 'Не указан', date: 'Дата / относительный срок', interval: 'Интервал', condition: 'Условие'});
    card.append(node('p', normalized ? `В сохранённой версии: ${normalized}` : 'Календарная дата не определена.', 'review-note'));
    field(card, 'Статус поручения', 'status', action.status, statuses);
    reviewControls(card, action);
    panel.append(card);
  }
  panel.append(node('h4', 'Краткие итоги'));
  const kinds = {fact: 'Факты', decision: 'Решения', action: 'Поручения', question: 'Открытые вопросы'};
  for (const [kind, title] of Object.entries(kinds)) {
    const items = data.result.summary.filter(item => item.kind === kind);
    if (!items.length) continue;
    panel.append(node('h4', title));
    for (const item of items) {
      const card = node('article', undefined, 'action-card');
      card.dataset.summaryId = item.id;
      field(card, 'Итог', 'text', item.text, null, 2000);
      field(card, 'Раздел', 'kind', item.kind, kinds);
      reviewControls(card, item);
      panel.append(card);
    }
  }
  if (!data.result.summary.length) panel.append(node('p', 'Модель не выделила содержательных итогов.'));
  panel.append(node('h4', 'Доклады по направлениям'));
  for (const item of data.result.reports || []) {
    const card = node('article', undefined, 'action-card');
    card.dataset.reportId = item.id;
    field(card, 'Направление / доклад', 'direction', item.direction, null, 200);
    field(card, 'Показатель (пусто — не указан)', 'indicator', item.indicator, null, 1000);
    field(card, 'Проблема (пусто — не указана)', 'problem', item.problem, null, 1000);
    reviewControls(card, item);
    panel.append(card);
  }
  if (!data.result.reports?.length) panel.append(node('p', data.upgrade_available
    ? 'Для таблицы показателей выполните анализ новой версии.' : 'Модель не выделила докладов с показателями или проблемами.'));
  if (document.querySelector('#analysis-history').open) loadAnalysisHistory();
}

const analysisFieldNames = {task: 'Поручение', assignee_text: 'Ответственный', due_text: 'Срок',
  due_kind: 'Тип срока', status: 'Статус', text: 'Текст', kind: 'Раздел', reviewed: 'Проверено', excluded: 'Исключено',
  direction: 'Направление / доклад', indicator: 'Показатель', problem: 'Проблема'};

analysisForm.addEventListener('input', event => {
  const card = event.target.closest('.action-card');
  if (!card) return;
  if (!event.target.matches('.item-reviewed')) card.querySelector('.item-reviewed').checked = false;
  analysisDirty = true;
  analysisReviewMessage.textContent = 'Есть несохранённые правки поручений и итогов. После исправления проверьте пункт повторно.';
  updateSaveButtons();
});

function collectAnalysisPatch() {
  const collect = (selector, key) => [...document.querySelectorAll(selector)].map(card => {
    const item = {id: card.dataset[key], reviewed: card.querySelector('.item-reviewed').checked,
      excluded: card.querySelector('.item-excluded').checked};
    for (const input of card.querySelectorAll('[data-field]')) {
      const name = input.dataset.field;
      item[name] = input.value.trim();
      if (['assignee_text', 'due_text', 'indicator', 'problem'].includes(name)) item[name] ||= null;
    }
    if ('due_kind' in item && ((item.due_text === null) !== (item.due_kind === 'unspecified'))) {
      throw new Error('Укажите текст и тип срока. Если срок отсутствует, очистите текст и выберите «Не указан».');
    }
    if (!(item.task ?? item.text ?? item.direction).trim()) throw new Error('Текст пункта не может быть пустым.');
    return item;
  });
  return {expected_revision: currentAnalysis.result.review.revision,
    actions: collect('[data-action-id]', 'actionId'), summary: collect('[data-summary-id]', 'summaryId'),
    reports: collect('[data-report-id]', 'reportId')};
}

analysisForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (!analysisDirty || analysisSaving || saving || dirty || analysisStale) return;
  const id = selected;
  const data = currentAnalysis;
  analysisSaving = true;
  analysisReadSequence++;
  updateSaveButtons();
  analysisReviewMessage.textContent = 'Сохранение…';
  try {
    const result = await api(`/api/meetings/${encodeURIComponent(id)}/analysis/${data.id}/review`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(collectAnalysisPatch())});
    if (selected !== id) return;
    analysisKey = null;
    renderAnalysis({...data, result}, false);
    analysisReviewMessage.textContent = `Поручения и итоги сохранены. Версия ${result.review.revision}.`;
  } catch (error) {
    analysisReviewMessage.textContent = error.status === 409
      ? `${error.message} Ваши правки остаются в полях; скопируйте их перед загрузкой сохранённой версии.` : error.message;
  } finally { analysisSaving = false; updateSaveButtons(); }
});

document.querySelector('#reload-analysis').addEventListener('click', async () => {
  if (analysisSaving || saving || (analysisDirty && !confirm('Заменить несохранённые поручения сохранённой версией?'))) return;
  const id = selected;
  analysisSaving = true;
  analysisReadSequence++;
  updateSaveButtons();
  try {
    const data = await api(`/api/meetings/${encodeURIComponent(id)}/analysis`);
    if (selected !== id) return;
    analysisKey = null;
    const stale = data.stale || (data.result && (data.source_digest !== currentResult.review.source_digest || data.revision !== currentResult.review.revision));
    renderAnalysis(data, stale);
  } catch (error) { analysisReviewMessage.textContent = error.message; }
  finally { analysisSaving = false; updateSaveButtons(); }
});

async function loadAnalysisHistory() {
  const id = selected;
  const analysisId = currentAnalysis?.id;
  if (!analysisId) return;
  const list = document.querySelector('#analysis-history-list');
  list.replaceChildren(node('p', 'Загрузка истории…'));
  try {
    const revisions = await api(`/api/meetings/${encodeURIComponent(id)}/analysis/${analysisId}/revisions`);
    if (selected !== id || currentAnalysis?.id !== analysisId) return;
    list.replaceChildren();
    if (!revisions.length) list.append(node('p', 'Ручных изменений пока нет.'));
    const value = v => v === null ? 'не указано' : (typeof v === 'boolean' ? (v ? 'да' : 'нет') : v);
    for (const revision of revisions) {
      const entry = node('article', undefined, 'history-entry');
      entry.append(node('p', `Версия ${revision.revision} · ${new Date(revision.changed_at).toLocaleString()}`));
      for (const change of revision.changes) entry.append(node('p',
        `${change.id} · ${analysisFieldNames[change.field] || change.field}\nБыло: ${value(change.old)}\nСтало: ${value(change.new)}`, 'history-change'));
      list.append(entry);
    }
  } catch (error) { if (selected === id && currentAnalysis?.id === analysisId) list.replaceChildren(node('p', error.message)); }
}
document.querySelector('#analysis-history').addEventListener('toggle', event => {
  if (event.target.open) loadAnalysisHistory();
});

function updateExportButton() {
  const available = currentAnalysis?.status === 'ready' && currentResult && !analysisStale;
  const blocked = dirty || saving || analysisDirty || analysisSaving || analysisPending || analyzing;
  const oldReports = document.querySelector('#export-template').value === '2' && currentAnalysis?.upgrade_available;
  document.querySelector('#download-docx').disabled = !available || blocked || oldReports || exporting;
  document.querySelector('#export-topic-options').hidden = document.querySelector('#export-template').value !== '1';
  for (const input of document.querySelectorAll('#export-topics input, #export-topics select')) {
    input.disabled = document.querySelector('#export-template').value !== '1' || input.dataset.first === 'true';
  }
}

function addExportTopic(first = false) {
  if (!currentResult?.segments.length) return;
  const row = node('div', undefined, 'action-card export-topic');
  const titleLabel = node('label', 'Название темы');
  const title = node('input', undefined, 'export-topic-title');
  title.maxLength = 200;
  title.required = true;
  title.value = first ? document.querySelector('#meeting-title').textContent : '';
  titleLabel.append(title);
  const startLabel = node('label', 'Начало темы');
  const start = node('select', undefined, 'export-topic-start');
  for (const segment of currentResult.segments) start.append(new Option(`${timecode(segment.start_ms)} — ${segment.text.slice(0,80)}`, segment.id));
  start.disabled = first;
  start.dataset.first = String(first);
  startLabel.append(start);
  row.append(titleLabel, startLabel);
  if (!first) {
    const remove = node('button', 'Убрать тему');
    remove.addEventListener('click', () => row.remove());
    row.append(remove);
  }
  document.querySelector('#export-topics').append(row);
}

function configureExport(id) {
  const key = `${id}:${currentResult.review.source_digest}`;
  if (exportMeeting !== key) {
    exportMeeting = key;
    document.querySelector('#export-organization').value = '';
    document.querySelector('#export-topics').replaceChildren();
    document.querySelector('#export-roles').replaceChildren();
    document.querySelector('#export-message').textContent = 'Сохраните исправления перед скачиванием.';
    addExportTopic(true);
  }
  const list = document.querySelector('#export-roles');
  const existing = Object.fromEntries([...list.querySelectorAll('input')].map(input => [input.dataset.speakerId, input.value]));
  list.replaceChildren();
  for (const person of currentResult.participants.filter(p => p.confirmed)) {
    const label = node('label', `${person.display_name} — должность (если известна)`);
    const input = node('input');
    input.dataset.speakerId = person.speaker_id;
    input.maxLength = 200;
    input.value = existing[person.speaker_id] || '';
    label.append(input);
    list.append(label);
  }
  if (!list.children.length) list.append(node('p', 'Сначала подтвердите имена участников в редакторе транскрипта.'));
}
document.querySelector('#export-template').addEventListener('change', updateExportButton);
document.querySelector('#add-export-topic').addEventListener('click', () => addExportTopic());
document.querySelector('#export-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (document.querySelector('#download-docx').disabled) return;
  const id = selected;
  const template = document.querySelector('#export-template').value;
  const payload = {template, organization: document.querySelector('#export-organization').value.trim(),
    topics: template === '1' ? [...document.querySelectorAll('.export-topic')].map(row => ({
      title: row.querySelector('.export-topic-title').value.trim(), start_segment_id: row.querySelector('.export-topic-start').value})) : [],
    roles: [...document.querySelectorAll('#export-roles input')].filter(input => input.value.trim()).map(input => ({speaker_id: input.dataset.speakerId, role: input.value.trim()})),
    analysis_id: currentAnalysis.id, source_digest: currentResult.review.source_digest,
    transcript_revision: currentResult.review.revision, analysis_revision: currentAnalysis.result.review.revision};
  exporting = true;
  updateExportButton();
  const output = document.querySelector('#export-message');
  output.textContent = 'Формирование DOCX…';
  try {
    const response = await fetch(`/api/meetings/${encodeURIComponent(id)}/export.docx`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    if (!response.ok) {
      const error = await response.json();
      throw new Error(typeof error.detail === 'string' ? error.detail : 'Проверьте параметры тем и документа.');
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = node('a');
    link.href = url;
    link.download = `Протокол-${id}-шаблон-${template}.docx`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    if (selected === id) output.textContent = 'DOCX сформирован и передан браузеру для скачивания.';
  } catch (error) { if (selected === id) output.textContent = error.message; }
  finally { exporting = false; updateExportButton(); }
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
    if (meeting.status !== 'ready') return;
    if (rendered === id) { await refreshAnalysis(id); return; }
    const result = await api(`/api/meetings/${encodeURIComponent(id)}/result`);
    if (selected !== id) return;
    renderResult(result, id);
    await refreshAnalysis(id);
  } catch (error) { document.querySelector('#list-message').textContent = error.message; }
  finally { refreshing = false; }
}
refresh();
setInterval(refresh, 3000);
