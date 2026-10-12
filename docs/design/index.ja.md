<!-- i18n-source: docs/design/index.md@blob:be3ee46a173bbb0a12af7858eff81ca38be27be5 -->

# 設計と研究

CPersona の挙動がどう決まったか、これからどこへ向かうかを記録したページです。インストールや日々の利用には必要ありません。使い始めるには[目的別ガイド](../index.md#where-to-go)から読んでください。

> **翻訳について**: 正本は英語版です。日本語版が古い場合や存在しないページは
> 英語版を参照してください (未翻訳ページは自動的に英語で表示されます)。

## これからの方向 { #where-it-is-going }

[ロードマップ](../roadmap.md) は、各リリースラインが何のためにあり、
何を破ってよく、計画中の機能がどの実測された問題に答えるかを記録します。軸は 3 つ
(リリースライン・ランタイムとスケール・サポート tier) です。これは意図の記述であって
納期の約束ではありません。実際に出荷されたものは[リリースノート](https://github.com/Cloto-dev/cpersona/releases)と
[SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md) にあります。

2.6 系には専用ページ [Reliable Recall](../RELIABLE_RECALL_2_6.md) があります。1 回の
呼び出しの中で反復する想起ループ、手がかりの契約、prior 関数、再構成の出口とその
件数の窓、失敗の分類、そして何をもって完了とするか。その完了条件の一つ一つを証拠と
突き合わせたページが [2.6 系の進捗](../PROGRESS_2_6.md) です。その次のラインにも専用ページ
[Memory Intelligence](../MEMORY_INTELLIGENCE_2_7.md) (2.7 系) があります。訂正と矛盾、
時間状態、証拠で重み付けした確信度、保持の方針、想起のフィードバック — 設計のみで、
決まっていない問いは未決定と明記してあります。

## プロジェクト標準 { #project-standards }

**プロジェクト標準** は、リリース・監査報告・生成されるポリシーブロックが
どうあるべきかを定めます。他のプロジェクトでも採用できます。

- [リリースライフサイクル標準](../RELEASE_LIFECYCLE_STANDARD.md) — ティア定義
  (Stable / Current)、リスク駆動の pre-release ladder、サポート期間。本リポジトリが
  実際に運用している内容は
  [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md) にあります。
- [SuperAuditor 標準](../SUPERAUDITOR_STANDARD.md) — クライアントがサーバーから
  findings を取得する方法。深刻度の語彙と、上限の意味。何を検出すべきかについては
  意図的に何も定めていません。
- [ポリシーブロック標準](../CLAUDE_MD_POLICY_STANDARD.md) — プロジェクトの skill が、
  エージェントが毎セッション読み込むファイル (`CLAUDE.md`、`AGENTS.md`、…) へ
  マーカー付きポリシーブロックを書き込む方法と、skill だけではブロックの存在を
  保証できない理由。

## 設計ノート { #design-notes }

**設計ノート** は、ある挙動がどう決まったかを、却下された経路も含めて記録します。
ある時点の記録です。ノートとガイドが食い違う場合はガイドが優先します。

- [クライアント別の権限 (ACL)](../ACL_DESIGN.md) — 名前付きベアラトークン、
  エージェント単位の読み書き権限、既定拒否。
- [OAuth 対応](../OAUTH_DESIGN.md) — リソースサーバーのメタデータ、トークン検証、
  比較した 3 経路、subject 単位の境界がどこに引かれるか。
- [サーバー供給の運用コンテキスト](../OPERATING_CONTEXT_DESIGN.md) — 接続中の全 MCP
  クライアントへ運用者の指示を配布する仕組み。
- [申告型セッション同一性](../SESSION_IDENTITY_DESIGN.md) — streamable HTTP では
  1 プロセスが 1 セッションにならない理由と、`session_key` がプロセス全体のどの状態を
  分割し直すか。
- [アクセス元の記録](../MEMORY_ORIGIN_DESIGN.md) — `agent_id` が誰も名指さない経路の
  ために、観測した呼び出し元を各行へ記録します。
- [recall プレビュー階層](../RECALL_PREVIEW_TIER_DESIGN.md) — プレビューの切り詰めと
  `get_contents` による展開経路。
- [埋め込み索引の連続配置](../CONTIGUOUS_INDEX_DESIGN.md) — ベクトル走査の読み出しを
  SQLite の行から連続配置の sidecar ファイルへ移します。答えは変わりません。
- [走査窓の到達範囲と新しさの優遇](../SCAN_WINDOW_REACH_DESIGN.md) — ベクトル走査窓を
  広げると最近の答えを失う理由と、新しさの優遇を捨てずに窓を広げるための 2 本目の
  順位付きリスト。
- [到達範囲・新しさ・far の票](../REACH_AND_RECENCY_PLAN.md) — 3 つの計測を 1 つに
  まとめ、それぞれ何を確立したか、そして 2.6 系で far の票に値段を付ける計画。
- [一本化した事前分布](../PRIOR_FUNCTION_DESIGN.md) — 位置と年齢の重みをすべて 1 つの関数に
  まとめ、順位だけに効かせて足切りには効かせないこと。confidence は並べ直しに使わず、
  各行の横に別の値として返すこと。
- [想起のプロセス v0](../RECALL_PROCESS_DESIGN.md) — 失敗をどの段で起きたかに帰属できる想起の記録と、
  指定した時期に見つかった行を、ゲートに触れずに上限付きの段数だけ上げる時期の手がかり。
- [証拠の配分](../EVIDENCE_ALLOCATION_DESIGN.md) — 窓の全レコードの引用候補を 1 つの順序に並べ、
  証拠の順に作った列を予算で切ること。予算を小さくした時に、各 item の決まった分ではなく
  答えている発話が残るようにします。
- [適応的融合](../ADAPTIVE_FUSION_DESIGN.md) — 各検索器にプールの取り分を確保すること、
  プールサイズ gate から順位カットを外すこと、そして「いま計測済みの lexical 重み」と
  「後の条件付き証拠の融合モード」のどちらを採るかを決める事前登録済みの比較。
- [Block による到達](../BLOCK_REACH_DESIGN.md) — 長いレコードを節に相当する Block へ分け、
  1 次元 1 ビットの Hamming 距離で順位付けし、親へ畳んで別枠で通すことで、
  品質 gate に新しいスコアを入れずに末尾へ到達できるようにすること。
- [1 ビット粗探索](../BINARY_COARSE_SEARCH_DESIGN.md) — 全レコードの 1 ビット索引を
  Hamming 距離で走査し、保存済みのベクトルで並べ直すことで、走査窓を広げずに、
  窓の外のレコードを答えの横に別枠で置き、時間の手がかりの期間を丸ごと探せるようにすること。
- [Block の候補生成の約束](../BLOCK_CANDIDATES_CONTRACT.md) — Block の腕の候補生成を、速い実装が
  同じ行を同じ順で返せる形で書いた約束と、それを守らせる golden、そしてその実装が SQLite でなく
  連続ファイルを SIMD のライブラリで読む理由。
- [溢れ分の tree](../OVERFLOW_TREE_DESIGN.md) — 長いレコードを埋め込みの窓に収まる区間に分け、
  recall が返すものを変えずに、返されたレコードを関連する部分で引用できるようにすること。
- [連想記憶](../ASSOCIATIVE_MEMORY_DESIGN.md) — entity・別名・関係の宣言されたグラフ。
  再構成想起が cue・束ねキー・根拠・roles として辿る。宣言が無ければ何も変わらない。
- [埋め込み劣化の通知](../DEGRADED_ADVISORY_DESIGN.md) — 埋め込み層が死んだとき、
  静かに質を落とすのではなく recall がそれを報告する仕組み。

## 研究ノート { #research-notes }

設計ページが拠って立つもの (導出・実測・反証) を、信じるのではなく検算できる形で
置いた記録です。[概要](../research/index.md) に status の語彙があります。

- [適応的融合の導出](../research/adaptive-fusion-derivation.md) — 各検索アームを、
  そのスコアが偶然で超えられる確率を通じて結合します。この規則は行ごとの影響度を
  閉じた形で持ち、現行の reciprocal rank fusion はその極限にあたります。
- [較正と admission floor](../research/calibration-admission-floor-2026-09.md) —
  recall が負けていた 7 タスクを 3 つの較正法で測りました。床は原因ではありません。
  null は誤ったペア母集団から取られていました。小さなコーパスでは dense アームが
  飢餓します。
- [損失はどこにあるか、凍結段リプレイ](../research/frozen-stage-replay-2026-09.md) —
  Track B 経路の全段を、凍結した埋め込みの上で 3 モデル分採点し、稼働中の pipeline に
  固定しました。純 ranking のタスクは融合段で、Gorilla は admission で、EPBench は
  gate で負けています。QASPER の利得は補充によるものです。
- [2 本のアーム、1 つの決定](../research/adaptive-fusion-identifiability.md) — その損失を
  避けるために融合則が知らねばならないもの。アームごとの較正ではなく、dense スコアを
  **与えたうえで** lexical がどれだけ情報を足すか、です。同時密度比はそれを、重みを
  当てはめずに供給します。飢餓・gate の全滅・予約の閉形式も導いています。
