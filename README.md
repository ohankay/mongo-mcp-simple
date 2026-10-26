# mongo-mcp-simple (`mongo-mcp`)

VS Code (GitHub Copilot Chat ve custom agent'lar) için **MongoDB (4.4 – 8.x)** MCP sunucusu. [sql-mcp-simple](https://github.com/ohankay/sql-mcp-simple) ile aynı yaklaşım: agent'larınız MongoDB işlemlerini mongosh / PowerShell çalıştırmadan, doğrudan bu sunucunun tool'larıyla yapar.

| | |
|---|---|
| Bağlantı | Kullanıcı adı + şifre (SCRAM), **Windows oturumu (Kerberos)**, LDAP, x509 sertifika, kimlik doğrulamasız ya da tam `mongodb://` / `mongodb+srv://` URI; standalone, replica set, sharded, Atlas |
| Okuma | `find`, `aggregate`, `count_documents`, `distinct`; Extended JSON (`{"$oid": ...}`, `{"$date": ...}`), **Türkçe collation** |
| Yazma | `insert_documents`, `update_documents` (operatör veya pipeline, `array_filters`, upsert), `replace_document`, `delete_documents` |
| Transaction | `run_transaction`: birden çok yazma işlemini ya hep ya hiç mantığıyla tek seferde çalıştırır (replica set / sharded) |
| Keşif | `list_databases`, `list_collections`, `infer_schema` (örnekten alan/tip çıkarımı), `list_indexes`, `collection_stats` |
| Yönetim | index oluştur/sil, koleksiyon/view oluştur (validator, capped, timeseries), yeniden adlandır, sil; `run_command` ile her admin komutu (kullanıcı/rol, `collMod`, `explain`, ...); `server_status`, `current_ops`, `kill_op` |
| Güvenlik | Salt okunur profiller, boş filtreyle toplu silme/güncellemede açık onay (`confirm_all`), drop işlemlerinde ad teyidi (`confirm_name`), şifreler hiçbir çıktıda görünmez |

## Kurulum (Windows)

### Hazır paket (önerilen, internet gerekmez)

1. Python'unuza uygun paketi indirip bir klasöre açın:
   - **64-bit Python:** [mongo-mcp-simple-windows-x64.zip](https://github.com/ohankay/mongo-mcp-simple/raw/main/release/mongo-mcp-simple-windows-x64.zip)
   - **32-bit Python:** [mongo-mcp-simple-windows-x86.zip](https://github.com/ohankay/mongo-mcp-simple/raw/main/release/mongo-mcp-simple-windows-x86.zip)

   Python'unuzun kaç bit olduğunu `py -c "import struct; print(struct.calcsize('P')*8)"` gösterir.
2. Klasörü VS Code'da açın, Copilot Chat → **Agent** moduna şunu yazın:
   `KURULUM_REHBERI.md dosyasını oku ve "Ajan için kurulum adımları" bölümünü sırayla uygula.`

Paketler her değişiklikte GitHub Actions üzerinde gerçek Windows makinelerde test edilir: 64 ve 32 bit için Python 3.10–3.14'te offline kurulum, kurulum sonrası paket doğrulaması, MCP stdio el sıkışması ve yanlış mimari tespiti.

### Elle kurulum (internet varsa)

```bat
py -m venv %USERPROFILE%\.mongo-mcp
%USERPROFILE%\.mongo-mcp\Scripts\pip install --prefer-binary "mongo-mcp-simple @ git+https://github.com/ohankay/mongo-mcp-simple.git"
```

Bağlantıyı test etmek için:

```bat
set MONGO_MCP_HOST=sunucu:27017
%USERPROFILE%\.mongo-mcp\Scripts\mongo-mcp.exe --check
```

## VS Code'a ekleme

`Ctrl+Shift+P` → **MCP: Open User Configuration**:

```json
{
  "inputs": [
    { "type": "promptString", "id": "mongo-password", "description": "MongoDB sifresi", "password": true }
  ],
  "servers": {
    "mongo": {
      "type": "stdio",
      "command": "${userHome}\\.mongo-mcp\\Scripts\\mongo-mcp.exe",
      "env": {
        "MONGO_MCP_HOST": "mongosrv01:27017",
        "MONGO_MCP_USER": "app_user",
        "MONGO_MCP_PASSWORD": "${input:mongo-password}",
        "MONGO_MCP_AUTH_SOURCE": "admin",
        "MONGO_MCP_DATABASE": "AppDb"
      }
    }
  }
}
```

Şifreyi VS Code bir kez sorar ve güvenli deposunda saklar. Hiçbir dosyaya düz metin olarak yazılmaz.

### Birden fazla sunucu (bazısı şifreli, bazısı Windows/Kerberos)

`%USERPROFILE%\.mongo-mcp\connections.json`:

```json
{
  "default": "test",
  "connections": {
    "dev":  { "host": "localhost:27017", "database": "AppDb" },
    "test": { "host": "mongotest01:27017", "user": "app_user", "password_env": "MONGO_PW_TEST",
              "auth_source": "admin", "database": "AppDb" },
    "prod": { "host": "m1:27017,m2:27017,m3:27017", "replica_set": "rs0", "user": "report_user",
              "password_env": "MONGO_PW_PROD", "database": "AppDb", "readonly": true,
              "read_preference": "secondaryPreferred" },
    "kurumsal": { "host": "mongoent01.sirket.local:27017", "auth": "windows", "database": "AppDb" }
  }
}
```

`mcp.json`: her `password_env` adı için bir `inputs` girdisi ve bir `env` satırı ekleyin:

```json
{
  "inputs": [
    { "type": "promptString", "id": "mongo-pw-test", "description": "TEST - app_user sifresi", "password": true },
    { "type": "promptString", "id": "mongo-pw-prod", "description": "PROD - report_user sifresi", "password": true }
  ],
  "servers": {
    "mongo": {
      "type": "stdio",
      "command": "${userHome}\\.mongo-mcp\\Scripts\\mongo-mcp.exe",
      "env": {
        "MONGO_MCP_CONFIG": "${userHome}\\.mongo-mcp\\connections.json",
        "MONGO_PW_TEST": "${input:mongo-pw-test}",
        "MONGO_PW_PROD": "${input:mongo-pw-prod}"
      }
    }
  }
}
```

`connections.json` içindeki `password_env` adı ile `mcp.json` `env` anahtarı **harfi harfine aynı** olmalıdır. Eşleşmezse sadece o profil "broken" görünür; diğer profiller çalışmaya devam eder ve hata mesajı `mcp.json`'a eklenmesi gereken satırı söyler.

Copilot'a "**prod** bağlantısında son 10 siparişi getir" demeniz yeterli; agent `list_connections` ile profilleri görüp `connection` parametresini kendisi seçer.

### Custom agent

`.github/agents/mongo-dba.agent.md` (bu repoda örneği var) içinde `tools: ['mongo/*']` yazmanız yeterli.

## Ayarlar

| Env (tek profil) | JSON (çok profil) | Varsayılan | Açıklama |
|---|---|---|---|
| `MONGO_MCP_HOST` | `host` | | `sunucu:27017` veya `m1:27017,m2:27017` |
| `MONGO_MCP_URI` | `uri` / `uri_env` | | `mongodb://` veya `mongodb+srv://` (şifre içeriyorsa `uri_env` ile verin) |
| `MONGO_MCP_AUTH` | `auth` | user varsa `scram`, yoksa `none` | `scram` / `ldap` / `windows` (Kerberos) / `x509` / `none` / `uri` |
| `MONGO_MCP_USER` / `MONGO_MCP_PASSWORD` | `user` / `password_env` | | Kimlik bilgisi |
| `MONGO_MCP_AUTH_SOURCE` | `auth_source` | `admin` (ldap/windows: `$external`) | Kullanıcının tanımlı olduğu veritabanı |
| `MONGO_MCP_DATABASE` | `database` | | Tool'larda `database` verilmezse kullanılan veritabanı |
| `MONGO_MCP_REPLICA_SET` | `replica_set` | | Replica set adı |
| `MONGO_MCP_DIRECT_CONNECTION` | `direct_connection` | | Replica set'te tek üyeye doğrudan bağlan |
| `MONGO_MCP_TLS` | `tls` | `false` | TLS (Atlas için URI zaten içerir) |
| `MONGO_MCP_TLS_ALLOW_INVALID_CERTIFICATES` | `tls_allow_invalid_certificates` | `false` | Şirket içi self-signed sertifikalar |
| `MONGO_MCP_TLS_CA_FILE` / `MONGO_MCP_TLS_CERTIFICATE_KEY_FILE` | `tls_ca_file` / `tls_certificate_key_file` | | CA dosyası / x509 istemci sertifikası |
| `MONGO_MCP_READ_PREFERENCE` | `read_preference` | | `primary`, `secondaryPreferred`, ... |
| `MONGO_MCP_READONLY` | `readonly` | `false` | Yazma tool'larını ve yazan komutları engeller |
| `MONGO_MCP_MAX_DOCS` | `max_docs` | `100` | Bir sonuçta dönen en fazla doküman (agent bağlamı şişmesin diye) |
| `MONGO_MCP_MAX_TIME_MS` | `max_time_ms` | `60000` | Sorgu zaman aşımı |
| `MONGO_MCP_SERVER_SELECTION_TIMEOUT_MS` | `server_selection_timeout_ms` | `10000` | Sunucuya ulaşma zaman aşımı |
| `MONGO_MCP_SERVER_MONITORING_MODE` | `server_monitoring_mode` | `poll` | Sürücünün sunucu izleme yöntemi. `poll`, uzun süre açık kalan bağlantıları kesen firewall/VPN ortamlarında güvenli seçenektir; `stream` sürücünün kendi varsayılanıdır |
| `MONGO_MCP_CONFIG` | | | Çoklu profil JSON dosyası |

**Windows oturumu (Kerberos):** `auth: "windows"` MongoDB **Enterprise** sunucularında Kerberos yapılandırılmışsa çalışır. Principal, Windows oturumundan (`KULLANICI@DOMAIN`) otomatik alınır, şifre gerekmez. Domain dışı bir bilgisayarda `user` alanına principal'ı yazın.

## Bilinmesi gerekenler

* **Türkçe arama:** regex `$options: "i"`, `İ`/`i` ve `I`/`ı` harflerini eşlemez. Büyük/küçük harf duyarsız Türkçe arama için `collation: {"locale": "tr", "strength": 2}` kullanın (`find`, `aggregate`, `count_documents`, `distinct`, `update_documents`, `delete_documents`). Türkçede `ş`, `ğ`, `ç`, `ö`, `ü` ayrı harflerdir; `AYSE`, `Ayşe` ile eşleşmez.
* **Özel tipler:** Sonuçlar relaxed Extended JSON döner (`{"$oid": "..."}`, `{"$date": "..."}`, `{"$numberDecimal": "..."}`). Bu değerler sonraki sorgularda filtre olarak aynen kullanılabilir.
* MongoDB'nin kendi `$toUpper` / `$toLower` operatörleri sadece ASCII harfleri dönüştürür ("Ayşe" → "AYşE").
* Ağ kaynaklı hatalarda (`_OperationCancelled`, `Could not reach`, `Lost the connection`) hata yanıtında `servers` listesi döner: her sunucunun sürücü tarafından görülen durumu, gecikmesi ve son hatası. Sorunun hangi üyede olduğu buradan görülür.
* `run_transaction` replica set veya sharded cluster gerektirir; standalone sunucuda açık bir hata döner ve hiçbir şey yazılmaz.
* Sunucu tarafı notu: MongoDB **8.0.x**, Linux çekirdeği 6.19 ve üzerinde başlamayı reddeder ([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)). 8.2, 7.0 ve öncesi etkilenmez. Bu MCP'yi değil, MongoDB sunucusunun kurulu olduğu makineyi ilgilendirir.

## Geliştirme ve testler

```bash
pip install -e ".[dev]"
docker compose -f tests/docker-compose.yml up -d
docker exec mongo-rs mongosh --port 27019 --quiet --eval "rs.initiate({_id:'rs0',members:[{_id:0,host:'localhost:27019'}]})"
export MONGO_MCP_TEST_TARGETS="mongo82=mongodb://root:Str0ng%21Pass@localhost:27082/;mongo70=mongodb://root:Str0ng%21Pass@localhost:27070/;mongo44=mongodb://root:Str0ng%21Pass@localhost:27044/"
export MONGO_MCP_TEST_RS="mongodb://localhost:27019/?replicaSet=rs0"
pytest -v
```

Test kapsamı:

* **Unit:** konfigürasyon doğrulama ve hata mesajları, bozuk profilin diğerlerini etkilememesi, her kimlik doğrulama tipi için istemci ayarları (Kerberos principal dahil), Extended JSON, salt okunur koruması, şablonlar ve `install.cmd`.
* **Entegrasyon:** MongoDB **8.2, 8.0, 7.0 ve 4.4** (kimlik doğrulamalı) ve replica set üzerinde CRUD, Türkçe collation, pipeline update, `array_filters`, duplicate key ve kısmi bulk hata, limit ve kesme, zaman aşımı, aggregate ve `$out`, şema çıkarımı, view, validator, capped, index'ler, `run_command`, düşük yetkili kullanıcıyla yetki hataları, `kill_op`, salt okunur profil, eşzamanlı çağrılar, transaction commit/rollback.
* **Uçtan uca:** `python -m mongo_mcp` gerçek MCP stdio protokolüyle (VS Code'un yaptığı gibi) başlatılır; bozuk profil, çok profilli şifre akışı, transaction ve `--check` CLI'ı test edilir.

## Lisans

MIT
