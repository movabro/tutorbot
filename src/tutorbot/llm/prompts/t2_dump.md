# T2 Knowledge Dump Assessment (v1)

사용자의 지식 덤프 텍스트를 읽고, 제공된 개념 인덱스에서 근거가 확인되는 개념의 이해 상태(`partial` 또는 `understood`)를 판정하세요.

규칙:
- 제공된 개념 인덱스에 있는 `concept_id`만 사용하세요.
- 근거가 있는 개념만 `assessments`에 포함하세요 (`evidence` ≤ 60자).
- 전체 커리큘럼 대비 부족한 영역 요약 `gap_summary`(최대 5줄)와 다음에 공부할 추천 개념 ID 목록 `next_concepts`(최대 5개)를 함께 출력하세요.
- 반드시 아래 JSON 스키마만 출력하세요:
```json
{
  "assessments": [{"concept_id": 12, "state": "partial|understood", "evidence": "≤60자"}],
  "gap_summary": ["≤5줄"],
  "next_concepts": [12, 15, 18]
}
```
