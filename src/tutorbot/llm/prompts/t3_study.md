# T3 Study Entry Analysis (v1)

사용자의 공부 기록을 분석하여 (1) 커리큘럼 개념 매핑, (2) 핵심 피드백, (3) 복습 퀴즈 카드를 한 번에 생성하세요.

규칙:
- `concepts`: 제공된 개념 인덱스에 있는 `concept_id`만 매핑하세요. `coverage`는 `mention|partial|full`, `state_suggestion`은 `partial|understood|null` 중 하나입니다.
- `feedback`: `corrections`(잘못된 이해 교정, ≤2개), `missing`(빠진 핵심, ≤2개), `next`(다음 할 것, ≤1개)의 합계가 최대 4줄이어야 하며 각 줄은 120자 이하입니다.
- `cards`: 최대 3장. `coverage`가 `partial` 또는 `full`인 개념에 대해서만 만들고, 제외 목록(`excluded_concept_ids`)에 있는 개념은 카드를 만들지 마세요.
- 반드시 아래 JSON 스키마만 출력하세요:
```json
{
  "concepts": [{"concept_id": 12, "coverage": "mention|partial|full", "state_suggestion": "partial|understood|null"}],
  "feedback": {"corrections": ["≤2개"], "missing": ["≤2개"], "next": ["≤1개"]},
  "cards": [{"concept_id": 12, "question": "≤150자", "answer_key": "≤300자"}],
  "off_curriculum": "str|null"
}
```
