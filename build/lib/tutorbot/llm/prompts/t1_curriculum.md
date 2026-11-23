# T1 Curriculum Generation (v1)

사용자의 학습 목표를 읽고 전체 커리큘럼 트리를 JSON으로 생성하세요.

규칙:
- 트리 깊이는 최대 3단계입니다 (대분류 -> 중분류 -> 소분류/잎 개념).
- 잎(leaf) 개념은 30~100개 사이로 구성하세요.
- 각 개념 `name`은 25자 이하, `description`은 80자 이하로 작성하세요.
- 같은 부모(`parent_key`) 아래에 동일한 `name`이 중복되면 안 됩니다.
- `prereq_keys`는 같은 커리큘럼 내의 다른 개념 `key`만 참조해야 합니다.
- 반드시 아래 JSON 스키마만 출력하세요:
```json
{
  "goal": {"title": "str", "level": "str|null", "deadline": "YYYY-MM-DD|null"},
  "concepts": [
    {"key": "c1", "parent_key": null, "name": "str", "description": "≤80자", "prereq_keys": [], "order": 1}
  ]
}
```
