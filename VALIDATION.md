# 검증 기록

2026-11-03, Linux x86_64, Python 3.14.7, OpenCode 2.2.5, Claude Code 2.1.317 기준입니다. 모든 실험·호스트 검사는 독립된 합성 프로젝트와 격리 설정을 사용했습니다. 실제 진행 중인 연구 프로젝트에는 쓰기·실험·설치를 수행하지 않았습니다.

| 검사 | 결과 | 기록 |
| --- | --- | --- |
| Python 단위·통합 검사 | 77개 통과 | `.plans/public-python-tests.log` |
| OpenCode bridge Node 검사 | 12개 통과 | `.plans/node-public-tests.log` |
| 실제 OpenCode 단일 invoke | `openai/gpt-6-luna` 요청 성공 | `.plans/evidence/provider-openai/` |
| 실제 조건부 루프 | durable provider 적용 후 합성 실험 3회·모델 호출 3회로 목표에서 stopped | `.plans/evidence/opencode-loop-durable-summary.json`, 해당 프로젝트 raw 기록 |
| 실제 Claude 호출 | 주간 한도 429를 실패로 감지 | `.plans/evidence/claude-quota.jsonl` |
| 실제 V2 plugin 등록 | 격리 서버에서 crucible active 확인 | `.plans/evidence/plugin-state.json` |
| wheel 생성 | runtime dependency 없는 wheel 생성 성공 | `.plans/wheel.log` |
| 설치된 패키지·프로젝트 연결 | 설치 CLI로 합성 실험 4회·호출 0회 완료, 생성 shim이 실제 V2 서버에서 active | `.plans/evidence/installed-and-native-summary.json` |
| ai-prompts 구조·launcher·격리 설치 | 통과 | `.plans/evidence/kit-harness.log`, `kit-install.log`, `kit-launcher.log` |
| 실제 수동 토론 | 독립 의견 2개·종합 1회 완료, 기존 실험 수·자동 루프 상태/예산 무변경 | `.plans/evidence/manual-live-summary.json` |
| 설치된 공통 스킬 | OpenCode 공통 탐색 경로와 Claude Code 경로에서 launcher/version/debate help 확인 | `.plans/evidence/installed-skills.json` |

자동 루프 구현 당시 wheel을 격리 환경에 설치해 CLI 예제와 프로젝트 연결부의 실제 V2 활성화를 확인했습니다. 수동 토론 추가 버전의 패키지 검사는 아래 게시 전 확인 기록에 별도로 남깁니다.

Python 검사에는 실제 subprocess 실험/evaluator, 비정상 종료·누락/변경 산출물·입력 변경·NaN·잘못된 결정, 예산, 중복 worker 잠금, pause/resume/cancel, worker SIGKILL 후 종료 영수증 회수, 불명확한 시작의 재실행 거부, 실패 단계 재시도와 성공 단계 재사용이 포함됩니다. Provider 전송 테스트는 모의 CLI로 비정상 종료+부분 텍스트·누락 terminal·denied tool·timeout 자식 정리를 검사합니다. Node는 argv/작업 경로 고정, 배달 실패 시 미승인 유지, 전달 후 ack, exit 2 상태 전달, bridge 취소 후 detached worker 생존을 검사합니다.

독립 검토에서 발견한 취소 후 잔존 자식, 제어기 종료 후 provider 감독 상실, active 설정·증거 변경 누락을 수정했습니다. TERM을 무시하는 자식의 실제 정리, 호출자 SIGKILL 후 provider actor의 timeout/receipt와 무중복 복구, 살아 있는 paused job의 설정 변경 시 재시도 거부, evaluator 이후 입력 및 모델 이후 설정·입력·artifact 변경 시 decision 미저장을 별도 회귀 검사로 확인했습니다. 관련 기록은 `.plans/provider-delta-tests.log`, `.plans/recovery-regressions.log`입니다.

독립 `adversarial_reviewer`의 최초 검토 한 번과 해당 수정의 표적 후속 검토 한 번을 수행했습니다. 후속 검토는 등록된 로컬 소프트웨어 실험·제한된 파라미터 제안이라는 범위에서 승인했으며, 남은 Required Fix는 없습니다. 실행 검사와 별도로 코드·근거를 읽은 검토이며 실제 연구 성능을 판정한 것은 아닙니다.

수동 토론 검사는 입력 경계, 동일한 독립 packet, 선택적 반론, 필수 응답 실패, 예산, 중단 후 성공 응답 재사용, 불명확한 호출의 재실행 거부, snapshot 변경 거부를 포함합니다. 실제 대기 중인 실험 worker와 수동 토론을 병행해 제어 상태가 유지되는 것도 검사했습니다. 실제 수동 토론은 두 OpenCode 모델을 요청한 것이며 Claude 성공을 의미하지 않습니다.

공개 전 수동 토론과 설치·문서 범위에 대해 독립 검토 및 한 차례 표적 후속 검토를 수행했습니다. 최신 실행의 큰 파일·바이너리는 전체 hash·크기·생략 사유로 보존하도록 수정하고 두 표적 검사를 추가했습니다. 후속 검토에서 해당 공개 범위를 승인했으며 남은 Required Fix는 없습니다.

실제 루프는 same-seed 합성 quadratic 예제에서 OpenCode catalog의 `openai/gpt-6-luna`와 `openai/gpt-6.1-sol`을 명시적으로 요청했습니다. 독립 의견 두 개와 planner 결정 한 개를 받아 다음 실험을 실행했고 loss < 0.001에서 종료했습니다. OpenCode JSONL에 실제 모델 ID가 없어 관측 모델은 null입니다. 두 provider의 서버 내부 모델 정체나 GPT·Claude 교차 토론 성공을 입증하는 기록은 아닙니다.

실제 Claude의 성공 응답은 계정 한도로 확인하지 못했습니다. 성공 parser는 설치된 CLI 계약과 모의 전송으로 확인했으며, 실제 오류 응답의 subtype이 success여도 is_error와 terminal_reason으로 실패 처리합니다. quota 해제 후 작은 실제 호출을 추가 확인할 수 있습니다.

macOS process identity는 ps 기반 구현과 모의 검사만 있으며 실제 Mac 실행은 확인하지 않았습니다. 모델 tool 제한은 OS sandbox가 아니고 사용자 신뢰 코드의 부작용을 가두지 않습니다. 불명확한 외부 실행은 멈추며 exactly-once를 보장하지 않습니다. Session synthetic 입장과 모델의 실제 메시지 소비는 다릅니다. 배달·ack 사이 장애에는 같은 사건 ID가 재전달될 수 있습니다.

검사 통과는 연구 결과의 타당성·연구 품질 향상·비용 절감을 입증하지 않습니다. 실제 연구 평가 정책, 비교 조건, 불확실성과 확인 실험은 프로젝트별로 설계해야 합니다. 현재 범위는 등록된 로컬 소프트웨어 실험과 제한된 파라미터 제안이며 임의 코드 생성·장비·분산 scheduler는 포함하지 않습니다.

원시 검증 자료는 이 checkout의 Git 제외 경로 `.plans/`에 보관했습니다. 공개 소스만 복제한 환경에는 로컬 계정·원시 호출 기록이 자동 복사되지 않습니다.
