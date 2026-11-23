# T2 Knowledge Dump Assessment (v2)

사용자의 지식 덤프는 사용자가 현재 알고 있다고 생각하는 내용을 쓴 것이며, 사실이라고 전제하지 마세요. 내용을 사실 확인하고, 개념별 이해 상태와 명확한 오개념을 평가하세요.

규칙:
- 제공된 개념 인덱스의 `concept_id`만 사용하세요. 개념 인덱스에 없는 ID를 만들지 마세요.
- 사용자의 문장을 무조건 맞다고 여기지 말고, 일반적으로 확립된 지식과 개념 설명에 비추어 확인하세요.
- 명백히 틀린 주장은 `issues`에 `verdict: "incorrect"`로 기록하고, 원래 주장(`claim`), 정정 내용(`correction`), 짧은 이유(`reason`)를 적으세요.
- 확실하게 맞고 틀림을 가리기 어렵다면 `verdict: "uncertain"`으로 표시하세요. 추측으로 정답을 만들지 말고, 이유와 확인이 필요한 점을 설명하세요. `uncertain`에는 정정을 넣지 않아도 됩니다.
- 가장 중요한 쟁점만 골라 `issues`는 최대 10개로 제한하세요.
- `assessments`에는 덤프에서 실제로 드러난 이해만 포함하세요. 정확한 근거가 있는 개념은 `partial` 또는 `understood`, 핵심 주장이 틀렸고 올바른 이해 근거가 없다면 `unknown`으로 판정하세요. 틀린 주장을 이해의 증거로 사용하지 마세요.
- 일부는 맞지만 오개념도 있는 개념은 최대 `partial`로 판정하세요. 충분하고 정확한 설명이 있을 때만 `understood`로 판정하세요.
- 한두 문장의 전체 평가 `summary`를 작성하세요.
- 전체 커리큘럼에서 보완할 점 `gap_summary`(최대 5줄)와 다음에 공부할 추천 개념 ID `next_concepts`(최대 5개)를 함께 출력하세요.
- 반드시 아래 JSON 스키마만 출력하세요:
```json
{
  "summary": "덤프의 전반적인 이해 수준과 핵심 결과",
  "assessments": [{"concept_id": 12, "state": "unknown|partial|understood", "evidence": "≤100자"}],
  "issues": [
    {
      "concept_id": 12,
      "verdict": "incorrect|uncertain",
      "claim": "덤프에 적힌 주장",
      "correction": "정정 내용 또는 uncertain이면 null",
      "reason": "판정 이유"
    }
  ],
  "gap_summary": ["≤5줄"],
  "next_concepts": [12, 15, 18]
}
```
