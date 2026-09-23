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

async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте заполнение полей.');
  return data;
}
function node(tag, text, className) {
  const el = document.createElement(tag);
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
  message.textContent = 'Загрузка и проверка записи…';
  try {
    const meeting = await api(`/api/meetings?${values}`, {method: 'POST', body: file,
      headers: {'Content-Type': 'application/octet-stream', 'Idempotency-Key': requestKey}});
    selected = meeting.id;
    location.hash = selected;
    message.textContent = 'Запись принята. Можно оставаться на странице или вернуться позже.';
    await refresh();
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
});
window.addEventListener('hashchange', () => {
  document.querySelector('#meeting-audio').pause();
  selected = location.hash.slice(1);
  refresh();
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
    const audio = document.querySelector('#meeting-audio');
    audio.src = `/api/meetings/${encodeURIComponent(id)}/audio`;
    const transcript = document.querySelector('#transcript');
    transcript.replaceChildren();
    for (const segment of result.segments) {
      const card = node('article', undefined, 'transcript-segment');
      const seek = node('button', `${timecode(segment.start_ms)} · ${segment.speaker_id || 'Говорящий неизвестен'}`);
      seek.addEventListener('click', () => {
        audio.currentTime = segment.start_ms / 1000;
        audio.play().catch(() => { document.querySelector('#meeting-status').textContent = 'Нажмите воспроизведение в плеере.'; });
      });
      card.append(seek, node('p', segment.text));
      if (segment.review_flags.length) card.append(node('p', 'Требует проверки: ' + segment.review_flags.map(f => flags[f] || f).join('; '), 'review-note'));
      transcript.append(card);
    }
    document.querySelector('#result-summary').textContent = result.segments.length
      ? `Фрагментов: ${result.segments.length}. Требуют проверки: ${result.statistics.segments_requiring_review}.`
      : 'Речь не обнаружена. Проверьте аудиозапись.';
    document.querySelector('#meeting-result').hidden = false;
    rendered = id;
  } catch (error) { document.querySelector('#list-message').textContent = error.message; }
  finally { refreshing = false; }
}
refresh();
setInterval(refresh, 3000);
