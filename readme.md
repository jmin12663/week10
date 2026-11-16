# 같이 타요 — 택시 파티 모집 서비스

원하는 시간, 같은 방향으로 가는 학교 사람끼리 택시를 나눠 타도록 모집글을 올리고, 최대 4명이 실시간 채팅으로 만날 위치를 정하는 웹서비스입니다.

- 배포 주소: `http://16.176.25.92 `
- 저장소: https://github.com/jmin12663/week10

## 1. 서비스 소개

**대상 사용자**: 등하교와 수업이 끝나는 시간, 금요일 하교, 시험 마지막 날처럼 같은 시간대에 역·터미널로 가는 학생을 대상으로 합니다.

**만든 이유**: 에브리타임 게시판으로 택시 인원을 구하면 원하는 시간대와 장소를 댓글·쪽지로 일일이 연락해야 하고, 인원이 찼는지 실시간으로 알 수 없습니다. 

**핵심 기능**

| 기능 | 설명 |
|---|---|
| 회원가입·로그인 | 아이디·비밀번호·학번·학과·이름으로 가입. 다른 사용자에게는 `학과 + 가린 이름(신*민)`만 보임. 학번은 같은 파티 참여자에게만 보임 (정산 확인용) |
| 내 정보 | 학과 수정, 비밀번호 변경, 회원 탈퇴 (탈퇴 시 개인정보만 지우고 지난 파티 기록에는 "탈퇴한 사용자"로 남김) |
| 파티 모집글 | 만나는 장소·목적지·출발 시간으로 생성. 작성자가 방장. 목적지·날짜·시간(○시 이후)으로 검색 |
| 목록 탭 | 모집 중 / 참여 중 / 지난 파티(출발 시간이 지난, 내가 참여했던 파티 최근 50개) |
| 파티 입장 | 최대 4명. 여러 명이 동시에 입장해도 4명을 넘지 않음. 목록의 인원(2/4 → 3/4)이 새로고침 없이 바뀜 |
| 실시간 채팅 | 참여자끼리 WebSocket 채팅. 입장·퇴장·수정 내용이 시스템 메시지로 자동 안내 |
| 방장 관리 | 모집글 수정·삭제(삭제 시 채팅방도 함께 삭제), 불참자 강퇴(강퇴된 사람은 재입장 불가) |

**규칙**
- 출발 시간은 지금 이후 7일 이내만 가능, 출발 시간이 지나면 자동 마감
- 방장은 나갈 수 없고 모집글 삭제만 가능 (작성자 = 방장 유지)
- 출발 시간이 지난 파티는 수정·나가기·강퇴 불가 (참여 기록 보존)
- 출발 전인 파티에 참여 중이면 탈퇴 불가

## 2. 프로젝트 구조

```
week10-repo/
├── compose.yaml          # api + nginx 컨테이너, data 볼륨
├── api/                  # Flask 백엔드 (port 5000, 외부 비공개)
│   ├── app.py            # 앱 생성, 세션 키·쿠키 설정, 블루프린트 등록, /api/health
│   ├── database.py       # SQLite 연결, 스키마(users·party·party_member·party_kick·message), KST 시간
│   ├── common.py         # 공통: 에러 응답, 입력 검사, 이름 가리기, login_required
│   ├── auth.py           # 회원가입·로그인·로그아웃·내 정보·비밀번호 변경·탈퇴
│   ├── parties.py        # 파티 목록·생성·수정·삭제·입장·나가기·강퇴·채팅 기록
│   ├── realtime.py       # WebSocket: 목록 화면(lobby)·채팅방 연결 관리, 브로드캐스트
│   ├── requirements.txt  # Flask, flask-sock
│   └── dockerfile
├── nginx/
│   ├── nginx.conf        # / 정적 파일, /api/ 와 WebSocket을 api로 프록시
│   ├── dockerfile
│   └── html/             # 프론트엔드 (빌드 없는 순수 HTML·JS)
│       ├── app.js        # 공통 함수: api() 호출, 로그인 확인, 날짜 표시, XSS 이스케이프
│       ├── style.css
│       ├── login.html · signup.html
│       ├── index.html    # 파티 목록 (탭·검색, lobby WebSocket으로 실시간 갱신)
│       ├── party-form.html  # 파티 만들기 / 수정 (?id= 있으면 수정)
│       ├── party.html    # 채팅방 (참여자 목록, 채팅, 방장 메뉴)
│       └── me.html       # 내 정보 (학과 수정, 비밀번호 변경, 탈퇴)
└── tests/
    ├── test_api.py       # REST API 시험 (동시 입장 포함)
    └── test_ws.py        # WebSocket 실시간 시험
```

**DB 테이블**

| 테이블 | 내용 |
|---|---|
| `users` | 계정 (아이디·비밀번호 해시·학번·학과·이름) |
| `party` | 모집글 (장소·목적지·출발 시간·현재 인원·방장). 인원은 `CHECK (0~4)` |
| `party_member` | 참여자 (`UNIQUE(party_id, user_id)`로 중복 입장 차단) |
| `party_kick` | 강퇴 기록 (재입장 차단) |
| `message` | 채팅·시스템 메시지 |

## 3. 사용 흐름

```
signup.html ─가입─▶ login.html ─로그인(세션 쿠키)─▶ index.html (목록)
                                                  │
              ┌───────────────┬───────────────────┼───────────────────┐
              ▼               ▼                   ▼                   ▼
     party-form.html     [입장] 버튼          [참여 중] 파티        me.html
      파티 만들기         POST members          클릭                 내 정보
              │               │                   │
              └───────────────┴───────▶ party.html?id=N (채팅방) ◀──┘
                                         ├─ 채팅 (WebSocket)
                                         ├─ 나가기 → index.html
                                         └─ 방장: 수정(party-form.html?id=N) · 강퇴 · 삭제
```

1. **회원가입·로그인**: `POST /api/users` → `POST /api/sessions`. 로그인하면 서명된 세션 쿠키(7일)가 발급되고, 이후 모든 API·WebSocket이 이 쿠키로 사용자를 확인. 로그인 안 한 상태로 다른 페이지에 들어가면 `login.html?next=...`로 이동 후 돌아옴
2. **모집글 목록**: `index.html`이 `GET /api/parties`로 목록을 받고 `/api/ws/lobby`에 연결. 누군가 파티를 만들거나 입장·나가면 `party_update`/`party_removed` 이벤트로 새로고침 없이 갱신
3. **파티 만들기**: `POST /api/parties` → 작성자가 방장이자 첫 참여자(1/4) → 채팅방으로 이동
4. **입장**: `POST /api/parties/{id}/members`. `UPDATE ... WHERE member_count < 4` 한 문장으로 정원 확인과 증가를 같이 해서 동시 입장에도 4명을 넘지 않음
5. **채팅방**: `party.html`이 `GET /api/parties/{id}`(정보·참여자), `GET .../messages`(기록)를 불러온 뒤 `/api/ws/parties/{id}`에 연결. 채팅만 WebSocket으로 보내고, 입장·나가기·수정·강퇴는 REST로 처리한 뒤 서버가 `system`·`members`·`party_updated` 이벤트를 방에 보냄
6. **방장 관리**: 수정(`PATCH`) → 변경 내용이 시스템 메시지로 안내 / 강퇴(`DELETE .../members/{userId}`) → 대상에게 `kicked` 이벤트 후 연결 끊김 / 삭제(`DELETE /api/parties/{id}`) → 방 전체에 `party_deleted` 후 목록으로 이동
7. **출발 후**: 출발 시간이 지나면 목록에서 빠지고 `지난 파티` 탭에 남음 (채팅 기록 열람 가능)

**REST API**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/health` | 상태 확인 |
| POST | `/api/users` | 회원가입 |
| POST / DELETE | `/api/sessions`, `/api/sessions/current` | 로그인 / 로그아웃 |
| GET / PATCH / DELETE | `/api/users/me` | 내 정보 / 학과 수정 / 탈퇴(비밀번호 확인) |
| PUT | `/api/users/me/password` | 비밀번호 변경 |
| GET | `/api/parties?destination=&date=&hour=&joined=1&past=1` | 목록·검색 |
| POST | `/api/parties` | 파티 생성 |
| GET / PATCH / DELETE | `/api/parties/{id}` | 상세(참여자만) / 수정·삭제(방장만) |
| POST | `/api/parties/{id}/members` | 입장 |
| DELETE | `/api/parties/{id}/members/me` · `/{userId}` | 나가기 · 강퇴 |
| GET | `/api/parties/{id}/messages?before_id=` | 채팅 기록 (50개씩) |

**WebSocket**

| 경로 | 받는 이벤트 | 보내는 이벤트 |
|---|---|---|
| `/api/ws/lobby` | `party_update`, `party_removed` | 없음 |
| `/api/ws/parties/{id}` | `chat`, `system`, `members`, `party_updated`, `kicked`, `party_deleted`, `error` | `{"type":"chat","content":"..."}` |

## 4. AWS 구성도

```
                 ┌──────────────────────── EC2 (보안 그룹: 80 허용) ────────────────────────┐
브라우저 ──HTTP──▶│  nginx 컨테이너 :80                                                        │
  (HTML·JS)      │   ├─ /            → 정적 파일 (html, js, css)                              │
          ◀─WS──▶│   └─ /api/, /api/ws/ → api 컨테이너 :5000 (Flask + flask-sock)             │
                 │                          └─ SQLite 파일 → data 볼륨 (/data/taxi.db)        │
                 └──────────────────────────────────────────────────────────────────────────┘
```

**요청 경로**
- 화면: 브라우저 → EC2:80 → nginx → 정적 파일
- REST API: 브라우저 → nginx `/api/...` → Flask
- 실시간: 브라우저 ↔ nginx `/api/ws/...` (WebSocket 업그레이드) ↔ Flask

**통신 방식을 나눈 이유**
- 상태를 바꾸는 작업(가입, 생성, 입장, 나가기, 강퇴, 수정, 삭제)은 **REST**: 요청마다 성공/실패가 HTTP 상태 코드로 바로 돌아와 오류 처리와 시험 기록이 명확함
- 채팅과 변경 알림은 **WebSocket**: 서버가 먼저 보낼 수 있어 메시지·인원 변화가 즉시 반영되고, 빈 요청이 반복되지 않음

**사용한 AWS 서비스와 선택 이유**

| 서비스 | 역할 | 선택 이유 |
|---|---|---|
| EC2 | Docker Compose로 nginx·api 컨테이너 실행 | 로컬에서 확인한 Compose 구성을 그대로 실행할 수 있음 |

**ELB·Auto Scaling 검토**

- **몰리는 상황**: 수업 종료·하교 시간에 목록 조회와 입장이 집중되는 경우
- **현재 미적용**, 이유:
  1. 실시간 연결(WebSocket)을 서버 메모리에서 관리하고, 데이터는 서버 안의 SQLite 파일에 저장한다
  2. 학교 단위 사용자 수에서는 EC2 1대로 처리 가능하고, ALB·추가 인스턴스는 사용자가 없는 시간에도 비용이 발생함
- **확장한다면 필요한 변경**
  - SQLite → RDS로 데이터 공유
  - 서버 간 메시지 전달용 Redis Pub/Sub(ElastiCache)를 추가
  - ALB는 WebSocket을 지원하지만, 이미 연결된 WebSocket은 새 서버가 추가돼도 기존 서버에 붙어 있으므로 새 연결부터 분산된다
  - Auto Scaling 그룹에 최대 서버 수를 두고 예산 알림으로 비용 상한 관리

## 5. 실행·배포

### 준비 할 것
- 배포 환경에 Docker, Docker Compose 설치
- 포트를 바꾸려면 프로젝트 폴더에 `.env` 작성: `WEB_PORT=8080` (기본값 80)
- compose의 `ADMIN_PASSWORD`는 이전 버전에서 쓰던 값으로 현재는 사용하지 않음

### 로컬 실행
```bash
docker compose config            # 설정 검사
docker compose up -d --build     # 빌드 후 실행
curl http://localhost/api/health # {"status":"ok", ...} 확인
```
브라우저에서 `http://localhost` 접속 → 회원가입 → 로그인


### EC2 배포
```bash
git clone https://github.com/jmin12663/week10.git
cd week10
sudo docker compose up -d --build
```

| 항목 | 값 |
|---|---|
| 외부 포트 | 80 (`WEB_PORT`) |
| nginx 컨테이너 | 80 |
| api 컨테이너 | 5000 (외부 비공개, nginx만 접근) |
| 데이터 | Docker 볼륨 `notice_data` → `/data/taxi.db`, 세션 키 `/data/secret_key` |

> 시험 스크립트는 시험용 계정과 파티를 만들기 때문에, 시연 전 데이터를 비우려면 `docker compose down -v` 후 다시 실행합니다.

