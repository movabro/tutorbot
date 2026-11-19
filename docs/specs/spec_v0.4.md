# TutorBot Gemini 요청 제한 및 재시도 명세서 v0.4

> v0.3의 다중 도메인 파이프라인을 유지하면서 Gemini 요청 경계에 중앙 rate limiter와 명시적인 오류·재시도 정책을 추가하는 독립 명세서다. 이 문서는 구현 대상과 검증 기준을 정의하며, v0.2 및 v0.3을 대체하거나 수정하지 않는다.
>
> 규범적 표현: **MUST**(필수), **SHOULD**(권장), **MAY**(선택).

## 1. 목표와 범위

### 1.1 목표

- Gemini 429(Too Many Requests)를 예방하기 위해 실제 HTTP 전송을 한 곳에서 제어한다.
- 논리적 LLM 작업이 아닌 **각 실제 Gemini HTTP 시도**를 제한한다. 최초 요청, 네트워크/HTTP 재시도, JSON 검증 실패 후 repair 요청 모두 같은 제한 경계를 통과한다.
- 서버가 제공한 재시도 지연을 우선하고, 유효한 서버 지연이 없을 때만 로컬 fallback을 사용한다.
- 429, 네트워크 오류, 일반 HTTP 오류를 구분하고 각 유형의 재시도 횟수와 종료 동작을 결정적으로 정의한다.
- 현재의 순차 파이프라인 및 동기식 호출 구조를 보존한다. 각 작업 함수에 임의의 `sleep`을 추가하는 방식은 사용하지 않는다.

### 1.2 범위

- Gemini 실제 HTTP 요청 경계의 rate limiter, 응답 오류 분류, 지연 파싱, 제한된 재시도.
- 기존 `LLMClient.complete_json()` 호출 경로의 최초 호출, retry, JSON repair 적용.
- 관련 설정 기본값·검증, 안전한 로그, 단위 테스트 및 수용 기준.

### 1.3 비목표

- 비동기/병렬 파이프라인 도입, 호출 순서 변경, 임의 동시성 최적화.
- Gemini 프로젝트·모델·티어의 실제 할당량을 추정하거나 보장하는 것.
- 일일 사용량, TPM, 과금 한도, 모델 선택 정책을 이 명세에서 조정하는 것.
- 모든 오류를 무한 재시도하거나 사용자 프롬프트/응답을 로그에 남기는 것.
- 이 명세 작업에서 production source, 테스트, 설정 파일 또는 v0.2/v0.3을 수정하는 것.

## 2. 현재 구현 사실과 목표 동작

### 2.1 현재 코드에서 확인한 사실

- `src/tutorbot/llm/client.py`의 `LLMClient.complete_json()`은 동기식 중첩 loop로 호출을 처리하며 `RETRY_BACKOFF = (2, 8)`을 사용한다.
- 현재 일반 예외 처리 경로는 예외 유형을 나누지 않고 최대 두 번 재시도한다. 따라서 네트워크, HTTP 및 그 밖의 예외가 동일한 retry 경로로 들어간다.
- `_call_gemini()`는 `urllib.error.HTTPError`의 상태 코드와 본문을 읽은 뒤 `RuntimeError("HTTP ...")`로 변환한다. 응답 헤더와 구조화된 Gemini 오류 정보는 상위 retry 로직에 전달되지 않는다. 다른 예외도 `RuntimeError("Network error: ...")`로 변환된다.
- JSON/스키마 검증 실패 시 `complete_json()`은 repair 지시를 추가하고 다시 호출한다. 이 요청은 별도의 실제 HTTP 요청이다.
- `src/tutorbot/pipeline.py`는 도메인 및 학습 작업을 순차 실행하고 하나의 `LLMClient`를 실행에 연결한다. 기존 순서를 바꾸지 않는 것이 목표다.
- 현재 retry 대기는 클라이언트의 주입 가능한 `sleep`을 사용하지만, 실제 HTTP 요청을 통합적으로 제한하는 중앙 rate limiter는 없다.

### 2.2 의도된 변경

위 항목은 현행 동작의 설명이지 목표 동작이 아니다. 목표에서는 HTTP 응답의 상태·헤더·오류 JSON을 보존 가능한 내부 오류 유형으로 전달하고, **Gemini HTTP 요청 직전의 공통 경계**에서 제한한다. 오류별 재시도 여부와 대기 시간은 §5에 따른다. 동기식 호출 및 순차 파이프라인은 그대로 유지한다.

## 3. 할당량과 기본 제한 정책

### 3.1 할당량 구분

- **RPM**은 일정 시간 구간의 요청 횟수 제한이다.
- **TPM**은 일정 시간 구간의 입력·출력 토큰 처리량 제한이다.
- **일일 할당량**은 날짜/사용량 기간 단위의 요청 또는 토큰 한도다.
- 이 명세의 limiter는 설정된 RPM 기반으로 요청 간격만 제어한다. TPM 또는 일일 한도를 계산·보장하지 않으며, 요청 본문 크기나 토큰 수가 작아도 RPM 제한은 별도로 적용된다.
- 관찰된 RPM=15 그래프나 과거 사용량은 **실제 API quota의 증거가 아니다**. 프로젝트, 모델, API 티어 및 시점에 따라 quota가 다를 수 있다. 15 RPM을 보편적인 Gemini quota로 간주하거나 코드에 하드코딩하지 않는다.

### 3.2 기본값과 간격

- 기본 설정은 **10 requests/minute**이며, 요청 사이의 최소 시작 간격은 `60 / 10 = 6초`다.
- **burst 없음**: 누적 토큰/permit을 쌓아 짧은 시간에 여러 요청을 보내지 않는다. 첫 요청도 같은 공통 경계에서 허용된 후 전송하고, 이후 요청은 최소 간격을 만족해야 한다.
- 간격 계산과 deadline은 벽시계가 아니라 주입 가능한 monotonic 시계를 사용한다.
- limiter는 같은 quota scope의 모든 Gemini 모델 요청에 공통 적용한다. 모델별로 limiter를 분리해 합산 요청률이 설정값을 초과하게 해서는 안 된다.
- 모든 실제 요청을 직렬화하는 단일 limiter 인스턴스를 실행에서 공유한다. limiter의 임계 구역은 실제 요청 시작 간격을 보장해야 한다. 네트워크 요청이 interval보다 오래 걸리면 다음 요청은 완료된 뒤 전송할 수 있으며, 호출 간격은 더 길어져도 된다.

## 4. 설정 형식과 검증

### 4.1 설정 데이터

새 옵션을 둔다면 `[llm]` 아래 정수 옵션 하나만 추가한다.

```toml
[llm]
requests_per_minute = 10
```

- 필드명: `llm.requests_per_minute`
- 자료형: 양의 정수. 설정 생략 시 기본값 `10`.
- 유효성: 1 이상의 정수만 허용한다. `0`, 음수, 실수, 문자열, boolean은 설정 로딩 시 명확한 설정 오류로 거부한다. `0`은 제한 비활성화로 해석하지 않는다.
- 설정된 값 `r`에 대해 최소 간격은 `60 / r`초다. 즉 기본값에서 6초이며, 사용자가 명시적으로 다른 양의 RPM을 설정하면 그에 맞춰 간격을 계산한다.
- 추가적인 burst, retry 횟수, sleep 시간 설정은 두지 않는다. 재시도 정책은 §5의 고정 정책을 따른다.
- 설정 검증 실패는 실행 중 조용히 기본값으로 대체하지 않고, Gemini 요청 전에 시작 실패로 노출한다.

### 4.2 범위와 공유

- 하나의 파이프라인 실행에서 사용하는 Gemini `LLMClient`는 하나의 limiter를 공유한다. 실제 HTTP 시도가 서로 다른 모델로 향해도 동일한 간격 정책을 적용한다.
- limiter는 프로세스 내 공유 객체다. 파이프라인의 기존 실행 잠금은 그대로 유지한다. 여러 독립 프로세스/호스트가 같은 API quota를 동시에 공유하는 배포에서는 프로세스 로컬 limiter만으로 합산 RPM을 보장할 수 없음을 운영 문서와 로그에 명시한다. 이 단계에서 분산 limiter를 새로 도입하지 않는다.
- 주입된 fake/backend 호출은 실제 Gemini HTTP가 아니므로 네트워크 대기 및 quota 제한 대상이 아니다. 단, 테스트는 호출 경계와 정책을 검증할 수 있도록 clock/sleep/transport를 주입할 수 있어야 한다.

## 5. 상세 처리 흐름 및 오류 정책

### 5.1 요청 흐름

1. `complete_json()`이 기존처럼 작업 모델과 메시지를 구성한다.
2. 실제 Gemini 요청인 경우 `_call_gemini()` 또는 동등한 transport 경로가 HTTP 요청을 보내기 직전에 중앙 limiter를 호출한다. 코드의 다른 계층이나 각 도메인 작업에 별도 sleep을 추가하지 않는다.
3. limiter는 monotonic 시각으로 다음 허용 시각을 계산하고, 필요한 만큼 주입된 sleep으로 기다린 후 실제 요청을 수행한다. 요청 시작 시각을 기준으로 다음 요청의 최소 간격을 보장하며 burst permit을 보관하지 않는다.
4. 응답을 성공 또는 유형화된 오류로 반환한다. HTTP 상태, 필요한 응답 헤더와 파싱 가능한 Gemini 구조화 오류 정보는 retry 판단에 전달한다.
5. 각 retry는 새 HTTP 요청이므로 다시 limiter를 통과한다. retry backoff 대기와 limiter 대기는 별개다. 먼저 retry 정책의 대기를 하고, 다음 실제 요청 직전에 limiter가 간격을 확인한다.
6. JSON 파싱/스키마 검증 실패로 repair 메시지를 추가하면 repair 호출 역시 2단계의 같은 limiter 경계를 통과한다. JSON 오류 자체는 HTTP 오류 retry로 오인하지 않는다.
7. 성공, retry 소진, 영구 오류는 기존 동기식 호출자에게 반환/예외 처리되어 파이프라인의 기존 오류 집계 흐름을 따른다.

### 5.2 중앙 limiter 요건

- 하나의 요청 경계에서 최초 호출·모든 HTTP retry·JSON repair를 빠짐없이 제한한다.
- 동일 limiter를 사용하는 동시 호출은 lock 등 thread-safe한 방법으로 실제 전송 시각을 보호한다. 동시 호출이 같은 시각에 함께 통과하는 race가 없어야 한다.
- 기다림은 주입된 `monotonic()` 및 `sleep(seconds)`를 사용한다. 단위 테스트에서 실제 벽시계 대기 없이 결정적으로 검증할 수 있어야 한다.
- rate limiter 자체의 예외는 잘못된 설정/내부 오류로 명시적으로 전달한다. 무한 대기, permit 누수, 자동 비활성화는 허용하지 않는다.
- 파이프라인 단계에 `sleep(6)` 같은 분산된 임의 대기 코드를 두지 않는다.

### 5.3 Retry-After 및 Gemini 재시도 지연

429를 받으면 대기 시간은 다음 우선순위로 결정한다.

1. HTTP `Retry-After` 헤더가 유효하면 우선한다. delta-seconds와 HTTP-date 형식을 처리한다. HTTP-date는 현재 UTC 벽시계와 비교해 남은 시간을 계산하되 과거 시각은 0초로 취급한다.
2. 헤더가 없거나 잘못된 경우 Gemini 오류 JSON의 구조화된 retry delay를 읽는다. 최소 지원 대상은 `error.details[]`의 Google `RetryInfo.retryDelay` duration(예: `"3s"`)이다. 인식하지 못하거나 잘못된 값은 없는 것으로 간주한다.
3. 유효한 서버 지연이 둘 다 없을 때만 로컬 fallback을 사용한다.

헤더와 body 중 유효한 값이 함께 있으면 헤더 값만 사용한다. 파싱할 수 없는 값 때문에 요청을 즉시 반복하거나 무한 대기하지 않는다. 서버가 준 유효한 지연을 임의의 짧은 fallback 값으로 덮어쓰지 않는다.

### 5.4 재시도 분류 및 횟수

재시도 예산은 `complete_json()` 논리 작업 전체에서 하나로 공유하지 않고, **payload variant별**로 관리한다. 최초 프롬프트가 첫 variant이며, JSON/스키마 검증 실패 후 생성되는 repair 요청은 두 번째이자 마지막 variant다. 각 variant는 transient HTTP/네트워크 오류에 대해 최대 3회의 실제 HTTP 시도(최초 시도 1회 + 재시도 최대 2회)를 허용한다. 따라서 repair가 없으면 논리 작업의 최악 HTTP 시도 횟수는 3회이고, repair가 발생하면 전체 최악 상한은 6회다. repair 요청은 최초 프롬프트 variant의 남은 재시도 예산을 공유하지 않고 별도의 동일한 제한 예산을 갖는다.

각 variant에서 HTTP 성공 또는 비-transient 오류가 발생하면 그 variant의 시도를 종료하며, transient 오류가 발생해도 3회 시도를 소진하면 더 요청하지 않는다. 최초 프롬프트의 성공 응답이 JSON/스키마 검증에 실패한 경우에만 repair variant를 정확히 한 번 생성한다. repair 응답도 JSON/스키마 검증에 실패하면 추가 repair를 만들지 않고 종료한다. 이 JSON 검증 실패는 그 자체로 HTTP 재시도를 소비하지 않는다.

| 실패 유형 | 재시도 정책 | 대기 |
|---|---|---|
| HTTP 429 | 해당 variant에서 최대 **3회 HTTP 시도**(최초 1회 + retry 최대 2회). 예산 소진 후 명시적 rate-limit 오류로 종료 | 다음 시도가 해당 variant의 3회 한도 안에 있을 때만, 해당 429 응답의 `Retry-After` → Gemini `RetryInfo.retryDelay` → fallback 순으로 대기. fallback은 `(2, 8)`초를 retry 순서대로 사용 |
| 네트워크/전송 오류(예: timeout, 연결 오류) | 해당 variant에서 최대 **3회 HTTP 시도**(최초 1회 + retry 최대 2회). 소진 시 오류 종료 | 다음 시도가 한도 안에 있을 때만 `(2, 8)`초 |
| HTTP 5xx | transient server 오류로 분류하고 해당 variant에서 최대 **3회 HTTP 시도**(최초 1회 + retry 최대 2회) | 다음 시도가 한도 안에 있을 때만 `(2, 8)`초 |
| HTTP 4xx 중 429 이외 | 즉시 종료. 잘못된 요청/인증/권한 오류 등을 무조건 반복하지 않음 | 없음 |
| 응답 JSON/스키마 검증 오류 | 최초 프롬프트 variant의 검증 실패 후 repair variant를 **정확히 1회만** 생성한다. repair 자체의 transient 오류는 repair variant의 별도 최대 3회 HTTP 시도 예산을 적용하며, repair 응답의 검증 실패로는 다시 repair하지 않는다. repair도 HTTP 요청이므로 limiter 적용 | repair의 HTTP 재시도 유형에 해당하고 다음 시도가 예산 안에 있을 때만 적용 |
| retry 횟수를 소진한 오류 | 성공처럼 변환하지 않고 안전한 오류 유형과 함께 기존 LLM 오류 경로로 전파 | 추가 대기 없음 |

- transient 오류 재시도는 각 payload variant의 3회 총 HTTP 시도 한도 안에서만 허용한다. 최초 프롬프트와 repair는 독립된 예산을 가지며, 한 variant의 성공·비-transient 오류·예산 소진은 그 variant의 추가 시도를 종료한다. 각 실제 HTTP 요청에는 limiter가 항상 적용된다.
- JSON/스키마 repair는 `complete_json()`당 최대 한 번이며, JSON 오류를 이유로 최초 payload를 무한 반복하거나 여러 repair variant를 만들지 않는다.
- 유효한 429 서버 지연은 fallback 순번을 소비하지 않는다. 서버 지연이 없는 해당 variant 내 연속 429 retry에 대해서만 2초, 이어 8초를 적용한다. 모든 Retry-After/body/fallback 대기는 **같은 variant에서 다음 HTTP 시도가 남아 있을 때만** 그 실제 재시도 전에 수행한다. 성공, 비-transient 오류 또는 마지막 허용 시도 후에는 재시도 목적의 대기를 하지 않으며, 대기나 limiter 통과를 이유로 시도 한도를 초과하는 HTTP 요청을 시작하지 않는다.
- retry 대기 중 취소/종료가 발생하면 다음 요청을 시작하지 않고 호출을 실패로 종료한다. 대기 재개를 위해 무제한으로 횟수를 초기화하지 않는다.

## 6. 오류·로그 및 민감정보 처리

### 6.1 오류 전달

- HTTP 오류를 일반 `RuntimeError` 문자열 하나로 평탄화하지 않는다. retry 계층이 최소한 HTTP status, 안전한 오류 분류, Retry-After 헤더, 필요한 구조화 retry delay를 판정할 수 있도록 내부 유형 또는 동등한 구조로 전달한다.
- 응답 본문은 retry delay 파싱에 필요한 범위에서만 메모리상 처리한다. 원문 본문 전체를 오류 문자열이나 DB에 넣지 않는다.
- 일반 네트워크 오류와 HTTP 오류를 별도 분류한다. 429는 5xx/연결 오류와 다른 정책 및 로그 코드로 남긴다.

### 6.2 로그

기존 `llm_call` 기록 구조를 우선 재사용하고 불필요한 DB 스키마 변경은 하지 않는다. 필요한 각 실제 HTTP 시도는 모델, task, 시도 결과, latency, HTTP status/error category를 진단할 수 있도록 기록한다. 대기 정보는 가능하면 `server_header`, `server_body`, `fallback`, `limiter`처럼 출처와 시간을 식별한다.

로그에 기록 가능한 항목:

- task 및 허용된 모델 식별자, HTTP status/error category, latency, 재시도 횟수, 대기 시간과 출처, token usage(응답에서 제공될 때).
- 안전하게 축약/정규화한 실행 참조 식별자. 사용자 입력이나 민감 문서 내용을 포함해서는 안 된다.

절대 기록하지 않을 항목:

- API key, 인증 토큰, key가 포함된 URL/query string, Authorization 헤더.
- 전체 요청/프롬프트/문서, 원시 Gemini 오류 body, 전체 응답 텍스트, 사용자 답변.
- 예외 문자열에 우연히 포함된 비밀정보. 오류는 분류 코드와 안전한 요약으로 치환/마스킹한다.

기존 DB의 `error` 필드에는 민감한 원문 대신 제한된 안전 오류 요약을 저장한다. 설정값이나 오류 메시지 마스킹 후에도 비밀이 노출되지 않는지 테스트한다.

## 7. 구성요소 및 파일별 구현 계획

이 표는 후속 구현 계획이다. 이번 v0.4 명세 작성 작업에서는 아래 production/test/config 파일을 수정하지 않는다.

| 파일/경로 | 구현 계획 |
|---|---|
| `src/tutorbot/llm/client.py` | typed HTTP 오류/429 헤더·body 지연 보존, retry 분류 및 기존 repair loop에 중앙 limiter 적용, 안전한 오류 요약 |
| `src/tutorbot/llm/rate_limit.py` 또는 `client.py` 내부 최소 구성 | thread-safe 요청 간격 limiter, monotonic/sleep 주입, quota scope별 공유 |
| `src/tutorbot/config.py` | `llm.requests_per_minute` 기본값 10, 양의 정수 검증 |
| `config.example.toml` | 선택적 설정 예시 `requests_per_minute = 10` |
| `src/tutorbot/cli.py` | 검증된 설정으로 limiter를 구성하고 하나의 pipeline 실행 내 Gemini client가 공유하도록 연결 |
| `src/tutorbot/pipeline.py` | 처리 순서 유지. 필요 시 limiter 구성 주입만 하며 단계별 sleep을 추가하지 않음 |
| `tests/` | 가짜 clock/sleep/transport를 사용한 limiter, HTTP 분류, retry, repair 및 로그 마스킹 단위 테스트 |
| `docs/specs/spec_v0.4.md` | 이 독립 명세서만 이번 작업에서 생성 |

파일 계획은 구현 시 최소 변경을 위한 안내다. 별도 요구가 없는 한 순차 구조, DB 스키마, 기존 task별 모델 선택과 처리 로직은 변경하지 않는다.

## 8. 테스트 및 수용 기준

모든 시간 관련 단위 테스트는 fake monotonic clock과 fake sleep을 사용해 실제 대기 없이 수행한다. HTTP 테스트는 transport 응답을 주입하며 실 Gemini API를 호출하지 않는다.

| 구분 | 수용 기준 |
|---|---|
| 설정 기본값 | 옵션을 생략하면 RPM=10, 계산된 최소 간격=6초, burst 없음으로 동작한다. |
| 설정 검증 | 0, 음수, 실수, 문자열, boolean은 로딩 시 거부되고 1 이상의 정수는 허용된다. |
| 무대기 첫 요청 | 초기 상태에서 첫 요청은 limiter에 의해 불필요하게 6초 대기하지 않으며, 이후 요청의 시작 간격은 6초 이상이다. |
| 단일 요청 간격 | 연속 호출이 fake clock에서 설정된 최소 간격을 만족하고, 누적 permit으로 연속 전송되지 않는다. |
| 동시성 | 같은 limiter에 대한 동시 호출도 실제 전송 간격을 지킨다. lock 경합에서 요청 누락·중복 permit·동시 통과가 없다. |
| 모델 공유 | 서로 다른 Gemini 모델/작업 호출이 하나의 실행 limiter를 공유해 설정 RPM을 우회하지 않는다. |
| 호출 경계 포괄성 | 최초 Gemini 요청, 네트워크/HTTP retry, JSON repair 요청이 각각 limiter를 통과한다. fake backend 호출은 실제 HTTP 제한 대상으로 취급되지 않는다. |
| Retry-After delta | 유효한 초 단위 헤더가 Gemini body 지연과 fallback보다 우선하며 정확한 가짜 sleep이 발생한다. |
| Retry-After 날짜 | HTTP-date 형식을 계산하고 과거 날짜는 0초로 처리한다. 벽시계/fake UTC를 이용해 재현 가능하다. |
| Gemini 지연 | 헤더가 없을 때 구조화된 `RetryInfo.retryDelay`가 사용된다. 잘못되거나 없는 값은 fallback으로 진행한다. |
| 최초 프롬프트 재시도 예산 | transient HTTP/네트워크 오류에서 최초 프롬프트 variant는 총 최대 3회 HTTP 시도(최초 1 + retry 2)를 한다. 성공, 비-transient 오류 또는 3회 소진 후 추가 시도가 없다. |
| Repair 재시도 예산 및 전체 상한 | 최초 응답의 JSON/스키마 검증 실패 시 repair variant를 정확히 한 번 생성한다. repair는 최초 variant와 독립된 총 최대 3회 HTTP 시도 예산을 가지며, repair 발생 시 `complete_json()` 전체 상한은 6회, 미발생 시 3회다. 두 variant 합산으로도 상한을 넘지 않는다. |
| 429 재시도 및 대기 | 각 variant가 최대 3회 시도를 넘지 않는다. 다음 시도가 허용되는 429에만 `Retry-After`/body/fallback 대기를 적용하고, fallback 2초/8초와 유효한 서버 지연 우선순위를 검증한다. 마지막 허용 시도 후에는 재시도 대기가 없고 추가 HTTP 요청도 없다. |
| 기타 transient 및 영구 오류 | 네트워크 및 5xx는 각 variant에서 최대 2회 retry한다. 429 이외 4xx는 즉시 종료하며, 성공/비-transient 오류/예산 소진 뒤 재시도하지 않는다. |
| JSON repair | JSON/스키마 오류는 repair를 한 번만 생성한다. repair 응답도 검증에 실패하면 두 번째 repair는 없으며, 두 variant의 retry 예산은 서로 공유하지 않는다. 각 실제 HTTP 요청은 limiter를 통과한다. |
| 안전 로그 | API key, query URL, 프롬프트, 오류 원문 body가 DB/로그에 남지 않고 status/category 및 안전한 요약은 진단 가능하다. |
| 파이프라인 회귀 | 처리 순서와 동기식 구조가 유지되고, 작업별 임의 sleep 없이 요청 경계에서만 제한한다. 기존 LLM 호출 성공/오류 반환 계약의 회귀 테스트가 통과한다. |
| 할당량 설명 | 문서와 설정 설명은 15 RPM을 보편 quota로 주장하지 않으며 RPM, TPM, 일일 quota를 구분한다. |

## 9. 위험 및 롤아웃

### 9.1 위험과 대응

- **quota 변동**: 10 RPM은 보수적 기본 운영 정책이지 Google의 보장 quota가 아니다. 배포 프로젝트/모델의 현재 quota를 별도로 확인하고 필요하면 설정을 명시적으로 조정한다.
- **공유 프로세스 범위**: 프로세스 로컬 limiter는 서로 다른 호스트/독립 프로세스의 총 요청률을 조절하지 않는다. 해당 배포 구조에서는 단일 실행 보장 또는 별도 분산 조정이 필요하며, 분산 기능은 이 범위 밖이다.
- **장시간 Retry-After**: 유효한 서버 지연을 존중하되 retry 횟수는 제한한다. 운영자는 대기 중인 실행의 종료/재시작 동작을 확인해야 한다. 서버 지연을 무시하고 즉시 재요청하지 않는다.
- **실패 지연 증가**: 429 및 네트워크 오류가 지속되면 호출과 배치 완료가 늦어질 수 있다. 시도 횟수/대기 출처를 민감정보 없이 로그로 확인한다.
- **민감정보 노출**: 현재 예외 본문 문자열은 그대로 예외/DB 필드로 흘러갈 수 있으므로 typed error 도입 시 로그 마스킹을 함께 검증한다.

### 9.2 롤아웃

1. 설정 생략 시 RPM=10으로 시작하고, 실제 프로젝트·모델 quota를 확인하기 전에는 15 RPM을 기본값으로 사용하지 않는다.
2. fake clock 및 가짜 HTTP 응답 테스트를 먼저 통과시킨다.
3. 단일 실행에서 모델별 호출이 공유 limiter를 사용하는지 검증한 뒤 배포한다.
4. 429 횟수, retry 대기 출처, 최종 실패 여부만 안전한 범위에서 관찰한다. API key, URL, prompt, 응답 원문은 수집하지 않는다.
5. 운영 중 RPM 설정 조정은 quota 확인과 관측 결과를 근거로 하며, TPM/일일 quota 문제를 RPM 조정만으로 해결했다고 간주하지 않는다.
6. 롤백 시 limiter 설정/코드를 되돌릴 수 있으나, 429를 구분하지 못하고 임의의 모든 오류를 재시도하는 현행 동작이 재도입될 위험을 검토한다.
