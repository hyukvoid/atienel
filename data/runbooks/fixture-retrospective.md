# 장애 재현 훈련 회고

## OOM 종료 코드 137

로컬 OOM fixture가 제한된 메모리에서 재시작했다. Pod UID와 컨테이너의 `lastState.terminated`에 기록된 OOMKilled, 종료 코드 137을 함께 확인했다. 원인 판단에는 종료 사유와 메모리 한도가 필요했다.

## 설정 오류와 재시작

CrashLoop fixture의 잘못된 시작 설정은 반복 실패와 Kubernetes BackOff Event를 만들었다. 직전 실행 로그로 최초 실패 지점을 확인하는 것이 재시작 횟수만 비교하는 것보다 원인 파악에 도움이 됐다.

## HTTP 500 급증

HTTP fixture에서 500 응답을 연속 재현하자 같은 Pod UID의 카운터가 10만큼 증가했다. Kubernetes Warning이 없어도 애플리케이션 카운터로 급증을 관측할 수 있었다. Pod 교체 이후에는 새 UID의 카운터로 기준을 다시 잡는다.
