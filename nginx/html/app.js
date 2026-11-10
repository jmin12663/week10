// 모든 페이지가 함께 쓰는 함수

// API 호출: 성공이면 JSON 반환, 실패면 서버의 error 메시지로 예외 발생
async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `요청 실패 (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
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

// 관리자 로그인 상태: 브라우저 탭을 닫으면 사라지는 sessionStorage에 보관
function getAdminPassword() {
  try { return sessionStorage.getItem('adminPassword') || ''; } catch (e) { return ''; }
}
function setAdminPassword(password) {
  try { sessionStorage.setItem('adminPassword', password); } catch (e) {}
}
function clearAdminPassword() {
  try { sessionStorage.removeItem('adminPassword'); } catch (e) {}
}

// 글 종류 라벨
function categoryChip(category) {
  return category === 'recruit'
    ? '<span class="type-chip recruit">모집</span>'
    : '<span class="type-chip notice">공지</span>';
}

// 남은 자리 배지 (모집글만)
function seatBadge(remaining, capacity) {
  if (capacity === null || capacity === undefined) return '';
  if (remaining <= 0) return '<span class="badge closed">마감</span>';
  return `<span class="badge open">${remaining} / ${capacity}석 남음</span>`;
}