<!-- i18n-source: docs/quality-assurance.md@blob:69e8cfa02230dc5a1ce54ba11a45de511454ebc9 -->

# 品質保証 { #quality-assurance }

このページは、cpersona のリリースがどのようにゲートされているかを説明します。読み手として想定しているのは、大切なコーパスをこのサーバーに預けてよいかを判断しようとしている人と、自分の変更がどの検査を通ることになるのかを知りたいコントリビューターです。

プルリクエストを出す前に検査を**実行する**方法を探しているなら、短い版が
[CONTRIBUTING § The gates](https://github.com/Cloto-dev/cpersona/blob/master/CONTRIBUTING.md#the-gates)
にあります。

## 監査でゲートされたリリース { #audit-gated-releases }

リリースを切る前に、コードベースは多エージェントによる包括監査ラウンドを通ります。観点ごとに独立した finder を立て、各 finding をさらに複数のレンズから敵対的に検証することで、「もっともらしいが誤っている」報告が修正まで生き残らないようにしています。v2.4.39 はそうしたラウンドを 3 周してから出荷しました — 43 件の修正、そのすべてを着地したツリーに対して再検証しています。

監査が生むのは finding であって修正ではありません。生き残った finding は、何かを編集する前にバグ台帳の採番済みエントリになります。こうすることで、コミットと台帳が `bug-NNN` の指す対象について食い違わなくなります。

## バグ台帳 { #the-bug-ledger }

監査済みの欠陥はすべて
[`qa/issue-registry.json`](https://github.com/Cloto-dev/cpersona/blob/master/qa/issue-registry.json)
に、機械検査可能なコードパターンとともに記録されます — その欠陥が何であったか、何によって再現するか、何が閉じたか。

[`scripts/verify-issues.sh`](https://github.com/Cloto-dev/cpersona/blob/master/scripts/verify-issues.sh)
は台帳をツリーと突き合わせ、修正マーカーが消えたり、除去したはずの欠陥が戻ってきたりすると声を上げて失敗します。これは read-only のインフラです — 台帳を検証するためのものであり、検査を通すために書き換えるものではありません。

## 構造 CI ゲート { #structural-ci-gates }

不変条件の中には、通常のテストでは表現できないものがあります。それが 1 つの挙動の性質ではなく、**すべての**呼び出し箇所の性質だからです。そうした不変条件は、pytest スイート内の AST レベル / 挙動レベルのゲートによって強制され、Python 3.11 と 3.13 の両方で実行されます:

- すべての writer が共有書き込みロックを保持していること
- agent スコープの SQL が isolation 述語を伴っていること
- identity / dedup のプローブが project 軸と channel 軸を伴っていること
- `check_health` がロック保持中に embedding のネットワーク I/O を行わないこと

この種のゲートは、ルールを忘れた呼び出し箇所そのもので失敗します。そこが「たまたま今日の呼び出し箇所を網羅しているテスト」との違いです。

## ドキュメント化された事実もゲートされる { #documented-facts-are-gated-too }

手書きの数値は腐ります。ドキュメントに書かれたツール数・スキーマバージョン・環境変数の既定値は、それを定義しているソースと突き合わせて検査されます。コードと食い違ったドキュメントは、読み手を誤らせる前に CI で落ちます。バージョンの主張はリリースタグと突き合わせて検査されます。日本語ページは翻訳元の英語コンテンツと突き合わせて検査されるため、翻訳が黙って原文から遅れることはできません。さらに、すべてのページと nav ラベルは翻訳を持つか「英語のままにする」と宣言するかのいずれかである必要があり、新しいページが翻訳済みサイトの外に黙って置き去りになることはありません。

これらのゲートは
[`scripts/`](https://github.com/Cloto-dev/cpersona/tree/master/scripts)
にあります: `check-docs-facts.py`、`check-doc-anchors.py`、`check-i18n-drift.py`、`check-i18n-coverage.py`。

## ミューテーション証明 { #mutation-proof }

スイートが緑であることは、テストが走ったことの証明であって、テストが気づいたはずであることの証明ではありません。isolation とロックの不変条件を担っている継ぎ目は CI で意図的に変異させられ、その各変異に対してスイートが赤になることを証明として要求します。変異させても緑のままのゲートは、合格ではなく**ゲート側の穴**として報告されます。

## リリースライフサイクル { #release-lifecycle }

リリースプロセス自体は
[RELEASE_LIFECYCLE_STANDARD](RELEASE_LIFECYCLE_STANDARD.md) (v1.0) で規定されており、Cloto 系プロジェクトのリファレンス実装としてここで試行されています。どのラインを動かすべきか、そのラインがいつまで修正を受け取るかは
[SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md)
にあります。

## 数字で見る { #by-the-numbers }

- **~18,100 LOC** の Python (機能ごとに分割されたモジュール群)、加えて 3,450 行の vendored MCP common スナップショット
- **~1,278 test functions** / ~111 test modules — 挙動マトリクスをパラメータ化すると ~1,618 cases (~37,280 LOC、サーバーコードよりテストコードの方が多い)。上記の構造強制ゲートを含む
- **Schema v13** (自動マイグレーション)
- **MIT License**

これらの数値は意図的に概数であり、それ自体がゲートされています — CI でツリーから再測定され、読み手を誤らせるほど乖離した時点で失敗します。
