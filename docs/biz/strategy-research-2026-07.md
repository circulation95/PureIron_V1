# PureIron 사업 전략 조사 (2026-07)

> 작성: pureiron-biz-strategist (cowork) · 검증 깊이 DEEP · 내부 의사결정용
> 근거: `docs/wiki/sot/product.md` · `docs/wiki/gtm/first-contract.md` + 외부 웹 리서치(출처 하단)
> 이 문서는 SOT가 아니다. 첫 계약 전략 정본은 `docs/wiki/gtm/first-contract.md`를 따른다.

## 요약

글로벌 SCADA 시장은 2026년 약 128억~148억 달러 규모로 조사기관별 연 6.3~9.3% 성장 전망이다. 경쟁 구도에서 PureIron의 위치는 "상용(Ignition 등)보다 가볍고 저렴하게, 오픈소스(FUXA·Rapid SCADA)보다 완성된 턴키 구축 서비스로"다. 국내에서는 2026년 스마트 제조혁신 지원사업의 AI 트랙(정부지원 50%, 최대 5억)이 PureIron의 Agent 스토리와 정확히 맞물리는 자금 채널이고, 스마트공장 구축 후 사후관리·활용 저조 문제(운영 애로 1·2위)가 월 구독 유지보수 모델의 시장 근거다.

## 1. 시장 규모·성장

- 글로벌 SCADA 시장 2026년 추정치: 128억 달러(Global Growth Insights, CAGR 6.5%) ~ 138.7억 달러(Fortune Business Insights, CAGR 8.5%) ~ 148.4억 달러(Business Research Insights, CAGR 6.3%). 기관별 편차가 크므로 "약 130~150억 달러, 연 6~9% 성장" 범위로 인용한다.
- 성장 동인: 클라우드·IIoT·엣지 분석 통합, 제조·유틸리티의 실시간 모니터링 수요 — PureIron의 웹 기반·경량·Agent 방향과 일치.
- 국내 중소 제조 SCADA/HMI 세분 시장의 공식 규모 통계는 이번 조사에서 확보하지 못했다(→ 확인 필요 항목).

## 2. 경쟁 구도

### 상용 벤치마크: Ignition (Inductive Automation)

- 서버 단위 영구 라이선스, 태그·클라이언트 무제한. 시작가 약 $1,620(추정 표기), 연 지원료는 소프트웨어 가격의 16~24%.
- 시사점: **라이선스 가격으로는 싸움이 안 된다.** Ignition 라이선스 자체가 이미 저렴하다. PureIron의 가격 anchor(구축 1,000만~3,000만 원/라인 + 월 50만~200만 유지보수)는 라이선스가 아니라 **한국 현장 턴키 구축 + LS·MELSEC 등 국산 프로토콜 + AI Agent + 한국어 지원을 포함한 총소유비용(TCO)** 프레임으로 제시해야 한다. Ignition은 국내 중소 현장에서 구축 SI·교육·유지보수 비용이 별도로 붙는다는 점이 비교 포인트다.

### 오픈소스 경쟁: FUXA · Rapid SCADA · ScadaBR

- FUXA(웹 기반 경량, Modbus·OPC-UA·S7), Rapid SCADA(C# 기반, 태그 무제한), ScadaBR(Java 기반) 모두 소규모 배포에는 성숙. 다만 업계 평가상 이중화·24×7 지원·알람 라이프사이클(ISA-18.2급) 도구에서 상용 대비 미흡.
- 시사점: "무료지만 직접 구축·운영해야 하는" 오픈소스 대비 PureIron의 차별점은 **3주 이내 단계형 유료 PoC 패키지, LS·AB·MELSEC 한국·아시아 프로토콜, Agent 기반 알람 원인 설명, 3D 트윈 데모, 조치 이력 시스템**이다. 오픈소스 표에서 PureIron이 겹치는 FUXA류와 기능표 비교자료를 영업용으로 준비할 가치가 있다.

### 국산·기존 HMI 벤더

- 엠투아이 등 국산 HMI/SCADA 벤더가 스마트팩토리 통합 솔루션으로 확장 중. 이들은 하드웨어(패널) 기반 설치가 강점이고, PureIron은 웹·모바일·Agent 계층이 강점 — 정면 경쟁보다 "기존 HMI 위에 얹는 관제 계층" 포지션이 SOT 방향과도 정합.

## 3. 국내 기회: 정부지원 + 사후관리 페인

### 2026 스마트 제조혁신 지원사업 (중기부 통합공고)

- 대·중소 상생형: 총 400억 규모, 일반 트랙 정부지원 30%·도입기업 최대 1.2억, **AI 트랙 정부지원 50%·최대 5억**. 신청 2026.01.02~05.29(예산 소진 시 조기마감).
- 시사점: PureIron의 Agent-assisted Troubleshooting은 AI 트랙 서사에 부합한다. **공급기업 등록 요건·절차 확인이 다음 액션.** 도입기업(고객)의 비용 부담을 30~50% 낮추는 영업 레버리지가 된다. 다만 첫 계약(3주 PoC)은 정부사업 일정과 무관하게 진행하고, 정식 구축 단계에서 지원사업을 결합하는 2단 구조가 2인 팀 리스크에 맞다.

### 사후관리·활용 저조 페인 (시장 근거)

- KDI 인식조사: 전환 최대 애로 '전환비용 부담' 46.6%, 운영 애로 '기존 시스템 호환성 부족' 24.0%, '유지·보수 등 사후관리 어려움' 23.0%.
- 정부도 구축 1년 이상 소기업 대상 사후관리 지원(긴급복구 500만/성장연계 2,000만, 630개사 내외)을 별도 운영 — 문제가 구조적이라는 방증.
- 시사점: SOT의 첫 계약 메시지("원인·조치·이력이 남지 않아 같은 문제가 반복된다")가 조사 데이터와 정합한다. **월 구독 유지보수 모델(월 50만~200만)은 이 사후관리 공백을 파는 것**이며, "구축하고 떠나는 SI"와의 차별 포인트로 제안서에 명시할 가치가 있다.

## 4. 전략적 시사점 (우선순위)

1. **TCO 비교자료 제작** — Ignition·오픈소스·PureIron 3열 비교(라이선스+구축+운영 3년 총비용, 프로토콜, 지원). 영업 데모 패키지에 포함.
2. **스마트공장 공급기업 등록 검토** — AI 트랙(50%, 5억) 활용 자격 확인. 등록 요건·기간이 2인 팀에 감당 가능한지 먼저 판단.
3. **유지보수 구독을 사후관리 페인에 명시적으로 연결** — 제안서 FAQ에 "구축 후 방치 문제" 항목 추가.
4. **오픈소스라는 정체성의 이중 활용** — 도입 장벽(락인 불안) 해소 메시지로 쓰되, 수익은 구축·유지보수·Agent 계층에서 발생함을 내부 가격 정책에 고정(SOT 가격 anchor와 정합).

## 확인 필요 항목 (검증 못 한 가정)

- 국내 중소 제조 대상 SCADA/HMI 세분 시장 규모 수치 — 공식 통계 미확보, KOSIS·산업연구원 자료 추가 조사 필요.
- Ignition 시작가 $1,620은 서드파티 추정 표기 — 공식 가격표(inductiveautomation.com/pricing)로 견적 기준 재확인 필요.
- 스마트공장 공급기업 등록 요건·PureIron 적격 여부 — 스마트제조혁신추진단 공고 원문 확인 필요.
- 정부지원 수치(400억·1.2억·5억)는 2026 공고 요약 기사 기반 — 신청 전 공고 원문(mss.go.kr) 대조 필요.

## 출처

- [Fortune Business Insights — SCADA Market](https://www.fortunebusinessinsights.com/scada-market-102433)
- [Business Research Insights — SCADA Market](https://www.businessresearchinsights.com/market-reports/scada-market-119383)
- [Global Growth Insights — SCADA Market](https://www.globalgrowthinsights.com/market-reports/scada-market-124959)
- [Inductive Automation — Ignition Pricing](https://inductiveautomation.com/pricing/edition) · [Capterra — Ignition](https://www.capterra.com/p/200185/Ignition-SCADA/) · [OperaMetrix — Ignition Licensing](https://www.operametrix.com/en/licensing/)
- [scadaprotocols.com — Free SCADA Software 2026](https://scadaprotocols.com/free-scada-software/) · [MDCplus — Open-Source SCADA](https://mdcplus.fi/blog/top-free-scada-manufacturing-automation/)
- [중기부 — 2026 스마트 제조혁신 지원사업 통합공고](https://www.mss.go.kr/site/smba/ex/bbs/View.do?cbIdx=310&bcIdx=1062885&parentSeq=1062885) · [2026 정부형 스마트공장 구축사업 공고](https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_000000000116263)
- [KDI e경제정보리뷰 — 스마트팩토리 중소기업 인식조사](https://eiec.kdi.re.kr/publish/reviewView.do?idx=88&ridx=8&fcode=000020003600004)
- [환경일보 — 스마트팩토리 도입 애로](https://www.hkbs.co.kr/news/articleView.html?idxno=668539)
- [헬로티 — 엠투아이 HMI/SCADA (AW 2026)](https://www.hellot.net/news/article.html?no=111165)
