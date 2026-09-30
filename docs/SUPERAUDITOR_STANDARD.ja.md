<!-- i18n-source: docs/SUPERAUDITOR_STANDARD.md@blob:39649f71b8cbf61bb86e8290c83c045b186cb937 -->

# SuperAuditor 標準

> **翻訳について**: 正本は英語版です。日本語版が古い場合は英語版を参照してください。

SuperAuditor 標準は、サーバーが自分の保存状態について見つけた問題を、すべての応答に
添えるのでなく、求められた時に正直な件数とともに渡す方法を定めます。標準は独立した
リポジトリ **[Cloto-dev/superauditor](https://github.com/Cloto-dev/superauditor)** に
移りました。正本の本文 ([STANDARD.md](https://github.com/Cloto-dev/superauditor/blob/main/STANDARD.md)、
v1.1)、応答のスキーマ、適合性の fixture、写して使う参照実装、動いているサーバーを外から
検査するチェッカーがそこにあります。v1 はこのページで最初に公開されました。

## CPersona での実装 { #how-cpersona-implements-it }

CPersona の `get_session_findings` ([ツール](tools.md)) は v1.1 に準拠し、そのことを
`_meta.superauditor` で示します。所見は `check_health` が計算するもので、検出器は 1 つです。

| 要件 | CPersona では |
| --- | --- |
| kind と severity | すべての health check が、固定の severity を持つ kind です。固定の規則で段階を上げる check は、段階ごとに別の kind で届けます: `null_embedding_expected` (info)、`null_embedding` (warn)、`null_embedding_pipeline_down` (critical)。 |
| 実行できなかった check | その check の所見の代わりに、check の名前を載せた `check_crashed` の所見 (warn) として届けます。他の check は届けます。 |
| 予約キー | check 自身の `kind` フィールドは `object_kind` に、check 自身が付けた `severity` は `health_severity` に移します。check がどちらかの名前を自分で出していたら、フィールドが黙って置き換わる代わりに呼び出しを拒否します。 |
| 分離 | データベース全体が対象です。所見は agent や project で絞らないので、このツールにはすべての agent を読む権限が要ります。 |
| broadcast | ありません。CPersona は他の応答に所見を添えたことがないので、移行の要件 (C9) は該当しません。 |

`conformance/superauditor/v1/` の fixture は、標準の `conformance/v1/` の写しです。

動いている CPersona を外から検査するには:

```bash
uvx --from git+https://github.com/Cloto-dev/superauditor superauditor-check -- cpersona
```
