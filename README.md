# ytmusic-mcp

YouTube Music を自然言語で整理するためのローカル stdio MCP サーバー。
[ytmusicapi](https://github.com/sigma67/ytmusicapi) 1.12.2 + MCP Python SDK 2.2.0。

> ytmusicapi は非公式 API です。Google の規約上グレーで、アカウント停止のリスクが
> ゼロではありません。承知のうえで使ってください。

## セットアップ

```bash
cd ~/workspace/ytmusic-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 認証（browser ヘッダ方式）

ytmusicapi には OAuth 方式と browser ヘッダ方式がありますが、**このサーバーは browser
ヘッダ方式を使います**。理由:

- OAuth は Google Cloud Console でプロジェクトと「TV および入力が限られたデバイス」用の
  OAuth クライアントを作る必要があり、発行されるトークンは YouTube Data API のスコープ。
  ライブラリ・高評価した曲・プレイリストの並べ替えは YouTube Music の内部エンドポイントを
  叩くため、OAuth トークンでは扱えないか不安定（検索が HTTP 400 になる既知の問題もある）。
- browser ヘッダ方式は ytmusicapi の全機能が使え、Google Cloud の設定も API クォータも不要。
  有効期間はブラウザのセッションが生きている間（ログアウトしなければ約 2 年）。
- このサーバー機にブラウザは不要です。ヘッダは**ログイン済みのブラウザがある別の端末**で
  取得し、SSH 経由でこのマシンのファイルに書くだけ。

### 手順

1. **ログイン済みのブラウザがある端末**（PC の Firefox 推奨 / Chrome でも可）で
   <https://music.youtube.com> を開き、ログイン状態にする。
2. 開発者ツール（Ctrl-Shift-I）→ Network タブ → 検索欄に `browse` と入力。
3. 画面を少しスクロールするか上部の「ライブラリ」を押すと、`browse?...` への
   **POST / ステータス 200** のリクエストが出る。それを選ぶ。
   - Firefox: 右クリック →「コピー」→「リクエストヘッダーをコピー」
   - Chrome/Edge: 「Headers」タブの "Request headers" を `accept: */*` 以降すべてコピー
4. **このマシン上で** `headers_raw.txt` を作り、コピーした内容をそのまま貼り付ける。

   ```bash
   cd ~/workspace/ytmusic-mcp
   nano headers_raw.txt     # ここに貼り付けて保存
   chmod 600 headers_raw.txt
   ```

   貼り付けが難しければ、`browser.json.example` を `browser.json` にコピーして
   `PASTE_AUTHORIZATION` と `PASTE_COOKIE` を手で埋めても構いません（手順 5 は不要）。

5. 変換して疎通確認する。成功すると `browser.json` ができ、`headers_raw.txt` は消えます。

   ```bash
   .venv/bin/python setup_auth.py headers_raw.txt
   ```

`browser.json` / `headers_raw.txt` / `.env` は `.gitignore` 済み。**絶対にコミットしない。**
Cookie を誰かに渡すとアカウントを操作されます。チャットにも貼らないこと。

セッションが切れたら（`Could not authenticate` が出たら）手順 1〜5 をやり直します。

## Claude への登録

```bash
claude mcp add ytmusic \
  --scope user \
  -- /home/shino3/workspace/ytmusic-mcp/.venv/bin/python /home/shino3/workspace/ytmusic-mcp/server.py
```

確認: `claude mcp list` / `claude mcp get ytmusic`。外すときは `claude mcp remove ytmusic`。

## ツール

読み取り（`readOnlyHint`）:

| ツール | 内容 |
|---|---|
| `search` | 曲・アルバム・アーティスト等を検索。追加に必要な `videoId` を返す |
| `list_playlists` | 自分のプレイリスト一覧と `playlistId` |
| `get_playlist` | プレイリストの曲一覧。削除・並べ替えに必要な `setVideoId` 付き |
| `get_liked_songs` | 高評価した曲 |
| `get_library_songs` | ライブラリに保存した曲 |
| `get_library_albums` | ライブラリに保存したアルバム |
| `whoami` | 認証されているアカウント名 |

探索（プレイリストを育てる用）:

| ツール | 内容 |
|---|---|
| `get_song_radio` | 1曲（またはプレイリスト）を種にラジオを展開し、似た曲を集める |
| `get_related_songs` | 関連曲・他の演奏・類似アーティスト・おすすめプレイリスト |
| `get_artist_overview` | アーティストの人気曲・アルバム・シングル・関連アーティスト |
| `get_history` | 最近聴いた曲 |
| `get_taste_profile` | アカウントの好みの傾向（アーティスト一覧） |

典型的な流れは
`search` → `get_song_radio` → `get_related_songs` → `add_songs_to_playlist`。
`search` は「ユーザーが名前を言った曲」しか見つけられないので、広げるときは
ラジオと関連曲を使います。

書き込み:

| ツール | 内容 |
|---|---|
| `create_playlist` | プレイリスト作成（既定は PRIVATE） |
| `add_songs_to_playlist` | 曲を追加 |
| `remove_songs_from_playlist` | 曲を削除（実際に入っている曲だけ消す） |
| `move_playlist_item` | 並べ替え（指定曲を別の曲の後ろへ／省略で先頭へ） |
| `edit_playlist_details` | タイトル・説明・公開範囲の変更 |
| `rate_song` | 高評価 / 低評価 / 解除 |

## 安全策

- `delete_playlist` は既定で**登録されません**。使うときだけ環境変数で有効化:
  `YTMUSIC_MCP_ALLOW_DELETE=1`。有効時も、現在のタイトルと完全一致する
  `confirm_title` を渡さないと削除しません。
- 1 回の呼び出しで追加・削除できるのは既定 100 曲まで（`YTMUSIC_MCP_MAX_BULK` で変更）。
- レスポンスはサムネイル等を落として整形済み。Claude のコンテキストを浪費しません
  （実測: `get_song_radio` 約0.8KB、`get_related_songs` 約3KB、`get_artist_overview` 約5KB）。
  アーティスト経歴など長文は 500 字で切っています。

## 環境変数

| 変数 | 既定 | 内容 |
|---|---|---|
| `YTMUSIC_AUTH_FILE` | `./browser.json` | 認証ファイルのパス |
| `YTMUSIC_MCP_ALLOW_DELETE` | 未設定（無効） | `1` で `delete_playlist` を有効化 |
| `YTMUSIC_MCP_MAX_BULK` | `100` | 1 回の一括操作の上限曲数 |
