# FmJev

Appleの `fm respond` を使うローカルのJev風HTTP API。Python環境は `uv` で管理し、Python 3.14を使います。外部Pythonパッケージへの依存はありません。動作する `fm` が必要です。

## 起動

```sh
cd fmjev
uv sync --locked
fm available
fm respond --no-stream 'Reply with hello.'
uv run --locked python -m fmjev
```

`http://127.0.0.1:8080` で待ち受けます。終了は Ctrl+C。

```sh
curl -sS http://127.0.0.1:8080/health
curl -sS http://127.0.0.1:8080/v1/systemone \
  -H 'Content-Type: application/json' \
  --data-binary @examples/request.json
```

`GET /health` はサーバーの生存確認のみで、推論の成功は保証しません。
ポートや待ち時間は `uv run --locked python -m fmjev --port 8081 --timeout 60 --request-timeout 180` で変更できます。
`--fm ./path/to/fm` で実行ファイルを指定できます。

## 入力と出力

`POST /v1/systemone` はJSONの `state` と `questions` を受け取ります。`model` は省略可能、指定する場合は `fmjev-fm` のみ。Jevの実モデルと混同しないため `jev-latest` は受け付けません。

```json
{
  "state": "返金してください。",
  "questions": {
    "refund": {
      "type": "noul",
      "instructions": "顧客は返金を求めていますか？"
    }
  }
}
```

結果の形（数値は例）:

```json
{
  "model": "fmjev-fm",
  "answers": {"refund": {"type": "noul", "noul": 0.95}},
  "metadata": {
    "backend": "fm",
    "probability_method": "model_self_report",
    "calibrated": false,
    "confidence_method": "1 - normalized_shannon_entropy",
    "question_execution": "isolated_sequential",
    "elapsed_ms": 1000
  }
}
```

| 質問 | criteria | answers内の値 |
| --- | --- | --- |
| choice | 候補名→説明のオブジェクト、2〜255候補 | choice, probabilities, confidence |
| score | 低→高の段階説明の配列、2〜10段階 | score, probabilities, confidence, legend |
| noul | 省略、またはtrue/falseをキーとする説明 | noul（Yesである推定確率） |

instructionsは文字列・オブジェクト・配列、criteriaの各説明はこれらまたはnullを受け付けます。
scoreの段階は0から始まり、scoreは確率加重平均です。choiceは最大確率の候補で、同率なら入力で先に宣言された候補になります。

## fmとの接続

質問ごとに独立した `fm respond --no-stream --schema ... --instructions ...` を起動します。
状態と質問は標準入力で渡し、シェルを介しません。質問IDはモデルへ渡しません。
schemaの候補フィールドは内部で `p0`, `p1`, ... に変換し、候補名や説明はプロンプトで伝えます。
生成後にコードで候補名を復元するため、回答の候補は入力の集合に限定されます。
schemaファイルは一時ディレクトリ内に作り、呼び出し後に削除します。入力・応答・キーをファイルやアクセスログへ保存しません。

確率は有限の0〜1の数値でなければエラーにします。合計と1の差が0.05以内のときだけ正規化し、それ以上の不整合や全ゼロは502を返します。自動再試行はしません。

## Jevとの違い

- JevのAPIの主要な入力・answers構造を参考にしています。完全なAPI/SDK互換ではありません。
- 確率はモデルが生成した数値で、校正されていません。トークン確率でもありません。
- confidenceは独自定義 `1 - H(p)/log(n)`。均等分布で0、一点集中で1。正解率ではなく分布の集中度です。Jevのconfidenceと同一とは限りません。
- 質問は独立したセッションで**順次実行**します。質問や候補を増やすと処理時間・コンテキスト使用量が増えます。Jevの並列推論性能は再現しません。
- トークン使用量を正確に取得していないため `usage` を返しません。
- 自動評価や外部Jev API呼び出しは行いません。`TYPESAFE_API_KEY` もローカルAPIには不要です。

参考: [Introduction](https://docs.typesafe.ai/introduction), [Choice](https://docs.typesafe.ai/primitives/choice), [Score](https://docs.typesafe.ai/primitives/score), [Noul](https://docs.typesafe.ai/primitives/noul), [Confidence](https://docs.typesafe.ai/confidence)

## 制限とエラー

ローカル試作用で、127.0.0.1だけにバインドします。認証はなく、CORSも許可しません。外部公開用のサーバーではありません。
1リクエストは最大256 KiB、最大32質問。推論リクエストは同時に1件で、処理中の追加リクエストは429です。
モデルのコンテキスト制限により、この上限内でも大きな入力は失敗する場合があります。
1質問のタイムアウトは60秒、全質問の合計は180秒です。途中で失敗した場合、部分的な回答は返しません。

| HTTP | 意味 |
| --- | --- |
| 400 | JSONや質問定義が不正 |
| 411 / 413 / 415 | Content-Lengthなし / 本文が大きすぎる / Content-Typeが不正 |
| 429 | 別の推論を処理中 |
| 502 | fm出力が不正 |
| 503 | fmを実行できない、または推論に失敗 |
| 504 | 推論タイムアウト |

エラー形式: `{"error":{"code":"invalid_request","message":"..."}}`

`fm available` が成功しても `ModelManagerError 1008` が出る場合があります。この開発環境ではサンドボックス内で失敗し、許可を得て外で同じコマンドを実行すると成功しました。通常のターミナルで上の最小推論を確認してください。

## テスト

```sh
uv run --locked python -m unittest discover -s tests -v
```

単体・HTTPテストは偽のバックエンドを用いるためモデル不要です。実モデルを含む確認はサーバー起動後に `examples/request.json` をPOSTしてください。

### 実モデルでの確認結果（2026-09-17）

日本語のサンプルをHTTP経由で実行し、3質問で約2.3秒でした。Choiceは `returns`、Scoreは `0.7`、Noulは `0.8` を返しました。
ただし入力は「返金ではなく交換」と明示しているので、返金希望の `noul: 0.8` は誤判定です。否定を重視する指示を加えても、この実行では改善しませんでした。
この確認はAPIと実モデルの接続が動作することを示しますが、判断精度の保証ではありません。特に日本語の否定表現と数値による確率推定は、用途別の評価が必要です。
