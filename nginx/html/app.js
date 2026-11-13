// 모든 페이지가 함께 쓰는 함수

// API 호출: 성공이면 JSON 반환(내용 없으면 null), 실패면 서버의 error 메시지로 예외 발생
async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const data = res.status === 204 ? null : await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error((data && data.error) || `요청 실패 (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// 로그인한 사용자 정보. 로그인 안 했으면 로그인 페이지로 보냄
async function requireLogin() {
  try {
    const me = await api('/api/users/me');
    const who = document.getElementById('who');
    if (who) who.textContent = `${me.department} ${me.name}`;
    const logoutBtn = document.getElementById('logout');
    if (logoutBtn) logoutBtn.addEventListener('click', logout);
    return me;
  } catch (e) {
    if (e.status === 401) {
      location.replace(`login.html?next=${encodeURIComponent(location.pathname + location.search)}`);
    }
    throw e;
  }
}

async function logout() {
  await api('/api/sessions/current', { method: 'DELETE' }).catch(() => {});
  location.href = 'login.html';
}

// WebSocket 주소: https로 접속했으면 wss, 아니면 ws
function wsUrl(path) {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}${path}`;
}

// 사용자가 입력한 글을 HTML로 넣을 때 태그가 실행되지 않게 변환
function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

// 결과 메시지 표시 (type: 'ok' 또는 'fail')
function showMessage(el, text, type) {
  el.textContent = text;
  el.className = `message ${type}`;
}

// 'YYYY-MM-DD HH:MM' → Date (한국 시간 기준)
function parseKst(text) {
  return new Date(text.replace(' ', 'T') + ':00+09:00');
}

// 출발 시간 표시: "11/20(목) 18:30"
const WEEKDAYS = ['일', '월', '화', '수', '목', '금', '토'];
function formatDepart(text) {
  const [date, time] = text.split(' ');
  const [, m, d] = date.split('-');
  const day = WEEKDAYS[new Date(`${date}T12:00:00Z`).getUTCDay()];   // 날짜만으로 요일 계산
  return `${Number(m)}/${Number(d)}(${day}) ${time}`;
}

// "40분 후 출발", "2시간 후 출발"
function untilDepart(text) {
  const minutes = Math.round((parseKst(text) - Date.now()) / 60000);
  if (minutes < 0) return '출발 시간 지남';
  if (minutes < 60) return `${minutes}분 후 출발`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}시간 ${minutes % 60 ? (minutes % 60) + '분 ' : ''}후 출발`;
  return `${Math.floor(hours / 24)}일 후 출발`;
}

// 채팅 시각 "18:31"
function formatClock(text) {
  return text.slice(11, 16);
}

// 인원 배지
function seatBadge(party) {
  if (party.is_expired) return '<span class="badge closed">출발 시간 지남</span>';
  if (party.is_full) return `<span class="badge closed">${party.member_count}/${party.capacity} 마감</span>`;
  return `<span class="badge open">${party.member_count}/${party.capacity}명</span>`;
}

// <input type="datetime-local">에 넣을 현재 한국 시간 'YYYY-MM-DDTHH:MM'
function kstInputValue(offsetMinutes = 0) {
  const d = new Date(Date.now() + 9 * 3600000 + offsetMinutes * 60000);
  return d.toISOString().slice(0, 16);
}
