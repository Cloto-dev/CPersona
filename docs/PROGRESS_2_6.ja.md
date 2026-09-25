<!-- i18n-source: docs/PROGRESS_2_6.md@blob:82b6e58384c63f93af363e3bf5e67fd892ef246e -->

# 2.6 系はいまどこまで来ているか

> **翻訳について**: 正本は英語版です。この日本語版は参照用の翻訳で、内容が食い違う場合は英語版が優先されます。

更新日: 2026-09-25。このページは、2.6 系がどこまで進んだかを、項目ごとに証拠つきで
示します。このラインが何を建て、何をもって完了とするかは
[ラインのページ](RELIABLE_RECALL_2_6.md) が、各リリースに何が入っているかは
[リリースノート](https://github.com/Cloto-dev/cpersona/releases) が述べます。このページと
リリースが食い違う場合、正しいのはリリースです。

2.6 系は pre-release のラインです。リリースは `2.6.0aN` として PyPI にあり、`--pre` を
付けて導入します。最新の final リリースは 2.5 系にあります
([SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md))。

## 4 つの状態 { #the-four-states }

以下の各行は、このうち 1 つだけを持ちます。設計が機能として読まれないように、分けて
あります。

| 状態 | 意味 |
| --- | --- |
| **リリース済み** | 公開された pre-release に入っている。誰でも導入して呼び出せる。 |
| **開発中** | `master` にコードはあるが、未リリースか、既定を決める測定がまだ無いままリリースされている。 |
| **研究中** | 仮説、実験、設計の比較の段階。サーバーのコードはまだ無い。 |
| **不採用・修正中** | 測って退けたか、誤りが見つかって設計し直している。これらの行はページに残します。 |

## ラインの完了条件に対して { #against-the-lines-completion-conditions }

番号は [「完了」の意味](RELIABLE_RECALL_2_6.md#10-what-done-means) に従います。

| # | 条件 | 状態 | 証拠 |
| --- | --- | --- | --- |
| 1 | 想起プロセスと Cued Recall がゲートの背後で出荷される | **リリース済み** 2.6.0a7、v0 | ループの基本形: 呼び出し側は `time_cue` (いつ頃か、確かさつき) を渡せます。サーバーはその期間も探し、そこで見つかった行を品質ゲートの後に決まった段数まで上げ、その検索だけが見つけた記録に 1 席を取り、期間に何も無ければ 1 回だけ広げます ([#325](https://github.com/Cloto-dev/cpersona/pull/325)、[設計](RECALL_PROCESS_DESIGN.md#2-the-loops-basic-form))。呼び出しごとの opt-in です。能力としてのリリースで、手がかり付きの問が十分にある評価セットができるまで精度の主張はしません。手がかりの伝播と多段の停止は後続です。 |
| 2 | 最後の再ソートの扱いが決まっている | **リリース済み** 2.6.0a7 | confidence を有効にしても、confidence スコアは recall を並べ直さず、ゲートにも使われません。`CPERSONA_CONFIDENCE_ORDERING=legacy` で戻せます。far の重みと年齢の重みは、ゲートが通したものの順序だけを決める一本化した事前分布で、計測まで恒等の既定値です ([#322](https://github.com/Cloto-dev/cpersona/pull/322)、[設計](PRIOR_FUNCTION_DESIGN.md))。 |
| 3 | 深さと件数が分離されている | 2.6.0a2 で **リリース済み** | [#274](https://github.com/Cloto-dev/cpersona/pull/274)。`CPERSONA_RECALL_DEPTH_FLOOR` の既定は `0` で、測定で深さが選ばれるまで 2.5 系の結合を保ちます。 |
| 4 | 再構成想起がツールとして存在する | 2.6.0a2 で **リリース済み**、a3 で拡張 | ツール本体: [#274](https://github.com/Cloto-dev/cpersona/pull/274)。深さより広さ、payload の予算: [#280](https://github.com/Cloto-dev/cpersona/pull/280)、[#286](https://github.com/Cloto-dev/cpersona/pull/286)。item の形の統一と、何を落としたかを言う応答: [#288](https://github.com/Cloto-dev/cpersona/pull/288)、[#289](https://github.com/Cloto-dev/cpersona/pull/289)、[#290](https://github.com/Cloto-dev/cpersona/pull/290)。これまでの測定: 上限 1〜10 の [count replay](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/measurements/results-reconstruct-v1-count-replay.md) (記録自身が「既定を選ぶものではない」と述べています) と、事前登録した [reader study](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/measurements/results-reconstruct-v1_1-reader.md)。後者は 1 回目では reader が受け取る量が減らず、2 つの変更の後の再測定が、18 問で登録済みの判定則を満たしました。既定の件数は今も契約上の選択であり、測定された最適値ではありません。 |
| 5 | 適応的融合が両方のモデルで素の embedding を上回る | **研究中** | 設計と、その根拠である段ごとの損失の分析: [#260](https://github.com/Cloto-dev/cpersona/pull/260)、[#261](https://github.com/Cloto-dev/cpersona/pull/261)、[設計記録](ADAPTIVE_FUSION_DESIGN.md)。サーバーのコードはありません。 |
| 6 | ベンチマーク上のすべての失敗にコードと再生できる trace がある | **開発中** | 2.6.0a7 でリリース: 求めに応じて `recall` と `reconstruct` が各段の残した行・落とした行・並べ替えを記録として返し、`benchmarks/` の道具が記録と正解の参照から確定した失敗コードを付けます ([#324](https://github.com/Cloto-dev/cpersona/pull/324)、[設計](RECALL_PROCESS_DESIGN.md#1-the-recall-trace))。すべてのベンチマーク上の失敗への適用はまだです。 |
| 7 | 精度・トークン・遅延・メモリのフロンティアが動いた | **研究中** | 未測定です。凍結した 2.5 の baseline に対して、最後に測ります。 |
| 8 | 2.5 の baseline が抱える品質上の負債が、閉じられたか理由つきで持ち越された | **開発中** | ベンチマークの走行がどの較正の下で測られたかを、走行と一緒に記録するようになりました: [#258](https://github.com/Cloto-dev/cpersona/pull/258)。他の項目は未着手です。 |

## 完了条件の外で、このラインがリリースしたもの { #released-in-this-line-beyond-the-conditions }

| リリース | 追加したもの | 証拠 |
| --- | --- | --- |
| 2.6.0a1 | 識別子を検索できるまま保つ全文検索の正規化 | [#271](https://github.com/Cloto-dev/cpersona/pull/271) |
| 2.6.0a3 | 長いレコードを node に分け、検索索引の外に保つ。reconstruct の引用は最良の node から取る。レコードの一部を `get_contents` で展開する | [#282](https://github.com/Cloto-dev/cpersona/pull/282)–[#287](https://github.com/Cloto-dev/cpersona/pull/287)、[設計記録](OVERFLOW_TREE_DESIGN.md) |
| 2.6.0a4 | 宣言された連想記憶: エージェントが述べるエンティティ・別名・関係。`reconstruct` はそれを手がかりとして読み、有界にたどる。`traverse` は宣言された近傍を返す | [#291](https://github.com/Cloto-dev/cpersona/pull/291)–[#295](https://github.com/Cloto-dev/cpersona/pull/295)、[設計記録](ASSOCIATIVE_MEMORY_DESIGN.md) |
| 2.6.0a7 | エピソード境界ペナルティを既定で無効化: セッションごとにエピソードの要約が残る記憶では、答えを持つ記録を順位の下へ押し下げていたため | [#320](https://github.com/Cloto-dev/cpersona/pull/320) |

連想の層はリリース済みで、既定では off です。既定にするかどうかは専用の A/B で決め
ますが、その結果はまだ記録されていません。

## 不採用・修正中 { #withdrawn-or-reworked }

| 何が | 何が起きたか | 証拠 |
| --- | --- | --- |
| 適応的融合の reference panel (最初の仕様) | null を特定できませんでした。指定した 2 つの密度が同じ分布だったため、定義した証拠では、混合をそれ自身の null から区別できませんでした。この定義の上にコードが書かれる前に、定義を置き換えました。 | [#262](https://github.com/Cloto-dev/cpersona/pull/262) |

## このページに無いもの { #what-is-not-on-this-page }

日付です。[ロードマップ](roadmap.md) は記述的な文書で、このページもそれに従います。
行が動くのは、日付が来た時ではなく、証拠ができた時です。
