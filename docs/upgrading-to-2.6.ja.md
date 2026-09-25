<!-- i18n-source: docs/upgrading-to-2.6.md@blob:0cad570ac28040e97287b4a960eed31b93696628 -->

# 2.5 から 2.6 への移行 { #upgrading-from-25-to-26 }

このページは、既存の 2.5.x のストアを、現在の 2.6 の pre-release である **2.6.0a7** まで
一度に移行する手順をまとめたものです。2.6 の各 pre-release は、リリースノートに自分の段の
手順しか書いていません。このページは、2.5.12 からの手順を 1 か所に集めます。

2.6 はまだ pre-release の系列です ([サポート方針](https://github.com/Cloto-dev/CPersona/blob/master/SUPPORT.md)
では Experimental)。opt-in で、final リリースの保証はありません。以下の手順は 2.6.0 final までに
まだ変わりえます。このページは pre-release ごとに更新し、final の時点で確定します。

## 始める前に { #before-you-start }

1. **データベースと較正ファイルをバックアップする。**
   [バックアップと復元](operations.md#backup-and-restore)に従ってください。稼働中のまま取れる
   `sqlite3 "$CPERSONA_DB_PATH" ".backup 'cpersona-backup.db'"` に加えて、データベースの外にある
   `<CPERSONA_DB_PATH>.calibration.json` も写します。2.5 に戻す手段は、このバックアップだけです
   ([2.5 に戻す](#going-back-to-25)を参照)。
2. **埋め込みサーバーを確かめる。** すでに保存されている記録の溢れ分のノードを作るには、トークン数を
   報告するサーバー (CEmbedding 0.8.0 以降) が必要です。古いサーバーは `count: null` を返し、
   これはゼロではありません。
3. **pre-release を明示してインストールする。** pip は自分からは pre-release を選びません:

   ```sh
   pip install 'cpersona==2.6.0a7'
   ```

## 最初の起動で起きること { #what-the-first-start-does }

2.6 は最初の起動で、データベースをスキーマ版 13 (2.5.x のすべて) からスキーマ版 17 まで、1 段ずつ
移行します。**保存済みの行は 1 行も書き換えません**: 各段はテーブルとトリガーを足すだけです。
失敗した段は完了として記録されないので、次の起動でやり直されます。

| スキーマ版 | 追加された版 | 足すもの | 後で作るものはあるか |
| --- | --- | --- | --- |
| 14 | 2.6.0a3 | `record_nodes`: 溢れ分の tree (埋め込み窓を超えた部分の断片) | **あり**: すでに保存されている長い記録のノード ([下記](#build-the-overflow-nodes)) |
| 15 | 2.6.0a4 | `entities`、`entity_aliases`、`entity_mentions`、`relations`: 宣言された連想 | なし |
| 16 | 2.6.0a5 | `record_blocks`: Block による到達 | Block による到達を on にした場合だけ ([下記](#optional-turn-on-block-reach)) |
| 17 | 2.6.0a6 | `record_block_vectors`: Block ごとのベクトル | Block による到達を on にした場合だけ |

2.6.0a1、2.6.0a2、2.6.0a7 はスキーマを変えていません。

## 最初の起動の後 { #after-the-first-start }

### ゲートの較正がやり直される { #the-gate-is-recalibrated }

2.6.0a7 は採点の版を変えたので、それより前の版が保存した較正は古いものとして扱われます。既定の
`CPERSONA_CALIBRATE_ON_MODEL_CHANGE=true` なら、サーバーは起動時に全体のしきい値を較正し直します。
それと `CPERSONA_AUTO_CALIBRATE` の両方が off の場合、古いゲートは適用されず、`calibrate_threshold`
を実行するまで `deep_check` が `stale_scoring_version` を報告します。

**エージェントごとの較正はやり直されません。** `calibrate_threshold` で特定のエージェント向けに
較正したしきい値やゲートは、古い採点の版とともに破棄されます。そのエージェントは、もう一度
`calibrate_threshold` を実行するまで、全体のしきい値と経験則のゲートに戻ります。
`set_recall_precision` で設定した好みは保たれます。

### 溢れ分のノードを作る { #build-the-overflow-nodes }

移行前に保存された長い記録には、作るまでノードがありません。それまでは記録の先頭から引用され、
`node_unavailable: no_nodes` がそう伝えます。1 回で 50 件を処理する health check で作り、
残りが無いと報告されるまで繰り返します:

```text
check_health(agent_id="<id>", fix=true, checks=["missing_nodes"])
```

この修復は記録を変更しないので、ロックされた記憶にも使えます。埋め込みモデルを変えた後は、
もう一度実行してください。

### 任意: Block による到達を on にする { #optional-turn-on-block-reach }

Block による到達は既定で off で、off の間は費用がかかりません (埋め込みの呼び出しも、行も、キューの
仕事もありません)。長い記録の埋め込み窓を超えた部分の本文を、検索で到達できるようにします
([設計](BLOCK_REACH_DESIGN.md))。

- `CPERSONA_BLOCK_BUILD_ENABLED=true` は Block の索引を作って保ち、すでに保存されている記録の
  上限つきのバックフィルを始めます。進み具合は `check_health` が `missing_blocks` として示し、
  `fix=true` で先へ進めます。
- `CPERSONA_BLOCK_RETRIEVAL_ENABLED=true` は、recall の中で索引を読みます。構築のゲートが必要です:
  何も埋めない索引を読む設定は起動時のエラーになります。ベクトル検索がリモートの場合は効果がありません。

2.6.0a5 で Block の構築を on にしていた場合、その Block の集合はベクトルを持たないので、同じ
バックフィルが作り直します。

## 変わった挙動 { #behaviour-that-changed }

配備が頼っているものと照らし合わせてください。特に断りのない限り、どれも off か、2.5 と同じです。

- **`limit` は返す行数です** (2.6.0a2)。融合がどこまで深く見るかは
  `max(limit, CPERSONA_RECALL_DEPTH_FLOOR)` で、floor の既定は 0 なので、順位は 2.5 と同じです。
- **`reconstruct` には `recall` と同じエージェントごとの読み取り権限が要ります** (2.6.0a2)。
  [アクセス制御](ACL_DESIGN.md)を設定している場合だけ関係します。
- **confidence は recall の並べ直しにもゲートにも使われなくなりました** (2.6.0a7)。
  `CPERSONA_CONFIDENCE_ENABLED=true` の場合、各行は今も `confidence` の値を持ちますが、順序は融合の
  順です。旧来の並べ直しでは上位に来ていたプロフィール行 (`update_profile`) は、最後に並び、`limit`
  が埋まると切り落とされることがあります。毎回エージェントに届けたい情報は、クライアントの指示
  ファイルかシステムプロンプトに書いてください。`CPERSONA_CONFIDENCE_ORDERING=legacy` で旧来の順に
  戻ります。
- **エピソード境界のペナルティは既定で off です** (2.6.0a7)。古いセッションを抑えるのに使っていた
  配備は `CPERSONA_EPISODE_PENALTY_ENABLED=true` を設定してください。

2.6 で新しく入り、求めない限り何もしないもの: `reconstruct` ツール、recall の trace (`trace=true`)、
時期の手がかり (`time_cue`)、`declare_associations` または `store` で宣言する連想。

## 結果を確かめる { #checking-the-result }

- `check_health(agent_id="<id>", checks=["missing_nodes"])` が、作るべき記録は残っていないと報告する。
- `deep_check` が `stale_scoring_version` を報告しない。
- 答えを知っている recall をいくつか試し、それが返ってくる。

## 2.5 に戻す { #going-back-to-25 }

移行の前に取ったバックアップ (データベースと較正ファイル) を復元してから、2.5.12 をインストールします。
**移行済みのデータベースを 2.5 で開かないでください。** 2.5.12 は新しいスキーマを「誤読するかもしれない」
という警告付きで開くだけで、スキーマ版 17 のデータベースを元に戻す手段はありません。
