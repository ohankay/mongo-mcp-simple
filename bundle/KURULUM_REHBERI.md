# mongo-mcp-simple: Kurulum Rehberi (GitHub Copilot ajanı için)

## Kullanıcı için (30 saniye)

1. Python'unuza uygun zip'i indirin ve bir klasöre açın, örneğin `C:\Araclar\mongo-mcp-simple`:
   - **64-bit Python** (çoğu bilgisayar): `mongo-mcp-simple-windows-x64.zip`
   - **32-bit Python**: `mongo-mcp-simple-windows-x86.zip`

   Hangisi olduğunu bilmiyorsanız: `py -c "import struct; print(struct.calcsize('P')*8)"` komutu `64` veya `32` yazar. Yanlış zip'i seçerseniz kurulum bunu söyler.
2. VS Code'da **File → Open Folder** ile bu klasörü açın.
3. Copilot Chat'i açın, modu **Agent** yapın ve şunu yazın:

   > `KURULUM_REHBERI.md dosyasını oku ve "Ajan için kurulum adımları" bölümünü sırayla uygula.`

4. Ajan size MongoDB adresini ve bağlantı tipini soracak, kurulumu kendisi yapacak.
   **Şifrenizi sohbete asla yazmayın.** Şifreyi VS Code ilk başlatmada güvenli bir kutuda soracak.

Gereksinimler: Windows 10/11 (64 veya 32 bit), Python 3.10–3.14 (`py --version` ile kontrol edin). İnternet gerekmez, tüm paketler `wheelhouse` klasöründe.

Not: Kurulum sırasında terminalde bir kez `install.cmd` çalışır. Kurulumdan sonra MongoDB işlemleri için mongosh veya PowerShell hiç kullanılmaz.

---

## Ajan için kurulum adımları

> Sen bu kurulumu yapan GitHub Copilot ajanısın. Adımları **sırayla** uygula. Her adımın sonucunu kullanıcıya kısa bir cümleyle bildir. Bir adım başarısız olursa sonraki adıma geçme; "Sorun giderme" bölümüne bak, çözemiyorsan kullanıcıya hatayı olduğu gibi göster.
>
> **Kesin kurallar:**
> - Kullanıcıdan şifre İSTEME; şifreyi hiçbir dosyaya, komuta, ortam değişkenine veya `.env` dosyasına YAZMA. Şifreler yalnızca `mcp.json` içindeki `${input:...}` girdileriyle VS Code tarafından sorulur.
> - Kullanıcının mevcut `mcp.json` dosyasındaki diğer sunuculara ve girdilere DOKUNMA; sadece `mongo` sunucusunu ve onun `inputs` girdilerini ekle/güncelle.
> - Paketleri elle `pip install` ile KURMA; kurulum yalnızca `install.cmd` ile yapılır.
> - Bu klasördeki dosyaları değiştirme (`templates` sadece kopyalanır).
> - Komutları VS Code'un varsayılan terminalinde (PowerShell) çalıştır.

### Adım 1: Paketleri kur

Bu rehberin bulunduğu klasörde (zip'in açıldığı kök klasör) çalıştır:

```powershell
cmd /c install.cmd; "EXIT=$LASTEXITCODE"
```

- Çıktıda `KURULUM TAMAM` ve `EXIT=0` görmelisin.
- Kurulum yeri: `%USERPROFILE%\.mongo-mcp`. Komut yolu: `%USERPROFILE%\.mongo-mcp\Scripts\mongo-mcp.exe`.
- Hata kodları: `10` = uygun Python yok, `11` = venv oluşturulamadı, `12` = paket kurulamadı, `13` = komut çalışmadı, `14` = zip ile Python'un mimarisi farklı (diğer zip gerekiyor), `15` = mevcut kurulum farklı mimaride, `16` = kurulum sonrası doğrulama başarısız. Ayrıntı için "Sorun giderme" bölümüne bak.

### Adım 2: Bağlantı bilgilerini kullanıcıdan al

Kullanıcıya şu soruları tek mesajda sor (şifre SORMA):

1. **MongoDB adresi:** `sunucu:27017`; replica set ise tüm üyeler virgülle (`m1:27017,m2:27017,m3:27017`) ve replica set adı; Atlas/SRV ise `mongodb+srv://...` adresi (şifresiz).
2. **Bağlantı tipi:**
   - `password`: kullanıcı adı + şifre (en yaygın; MongoDB SCRAM)
   - `windows`: Windows oturumu ile Kerberos (sadece MongoDB Enterprise + Kerberos yapılandırılmış sunucularda)
   - `ldap`: LDAP kullanıcı adı + şifre (MongoDB Enterprise)
   - `none`: kimlik doğrulama yok (yerel geliştirme)
3. **Kullanıcı adı ve `auth_source`:** kullanıcının oluşturulduğu veritabanı; bilinmiyorsa `admin`.
4. **Varsayılan veritabanı** (tool'larda `database` verilmezse kullanılır).
5. **TLS gerekli mi?** (Atlas ve bazı kurumsal sunucular). Şirket içi sertifika güvenilir değilse `tls_allow_invalid_certificates`.
6. **Salt okunur mu?** Prod için `evet` önerilir.
7. **Birden fazla sunucu mu?** Evetse her biri için 1–6 tekrarlanır ve her birine kısa bir ad verilir (`test`, `prod`, ...).

### Adım 3: Bağlantıyı test et (yalnızca `none` ve `windows` tipi için)

Şifreli tiplerde bu adımı ATLA; test Adım 6'da VS Code içinden yapılacak.

```powershell
$env:MONGO_MCP_HOST = "<ADRES>"; $env:MONGO_MCP_AUTH = "<none|windows>"; $env:MONGO_MCP_DATABASE = "<VERITABANI>"
& "$env:USERPROFILE\.mongo-mcp\Scripts\mongo-mcp.exe" --check; "EXIT=$LASTEXITCODE"
Remove-Item Env:MONGO_MCP_HOST, Env:MONGO_MCP_AUTH, Env:MONGO_MCP_DATABASE
```

- `EXIT=0` ise kullanıcıya `server.version`, `server.topology` ve `server.authenticated_users` değerlerini bildir.
- `EXIT=1` ise `connections.default.error.message` mesajını göster ve "Sorun giderme" bölümüne bak.

### Adım 4: VS Code MCP yapılandırmasını yaz

Hedef dosya, **kullanıcı düzeyi** (tüm workspace'lerde geçerli): `"$env:APPDATA\Code\User\mcp.json"`
(VS Code Insiders: `$env:APPDATA\Code - Insiders\User\mcp.json`.)

| Durum | Şablon |
|---|---|
| Tek sunucu, kullanıcı adı + şifre | `templates/mcp.password.json` |
| Tek sunucu, Windows (Kerberos) | `templates/mcp.windows-auth.json` |
| Birden fazla sunucu | `templates/mcp.multi-profile.json` + `templates/connections.json` |

Kurallar:

- Yer tutucuları kullanıcının cevaplarıyla değiştir: `SUNUCU:27017`, `KULLANICI`, `VERITABANI`. `auth_source` farklıysa `MONGO_MCP_AUTH_SOURCE` değerini değiştir. Replica set için `"MONGO_MCP_REPLICA_SET": "<ad>"`, TLS için `"MONGO_MCP_TLS": "true"`, salt okunur için `"MONGO_MCP_READONLY": "true"` ekle. LDAP için `"MONGO_MCP_AUTH": "ldap"` ekle.
- **Dosya yoksa:** şablonun tamamıyla oluştur.
- **Dosya varsa:** `servers` altına yalnızca `"mongo"` anahtarını ekle (varsa güncelle); şablondaki `inputs` girdilerini, aynı `id` yoksa, mevcut `inputs` dizisine ekle.
- **Çoklu sunucu:**
  1. `templates/connections.json` dosyasını `%USERPROFILE%\.mongo-mcp\connections.json` olarak kopyala ve `connections` içini kullanıcının sunucularıyla doldur. Şifreli her profilde `password` alanı OLMAZ; `"password_env": "MONGO_PW_<PROFIL>"` yazılır (örn. `MONGO_PW_PROD`).
  2. `mcp.json` içinde **her** `password_env` adı için iki şey ekle: `inputs` dizisine bir `promptString` girdisi (`"id": "mongo-pw-<profil>"`, `"password": true`) ve `mongo` sunucusunun `env` bölümüne `"MONGO_PW_<PROFIL>": "${input:mongo-pw-<profil>}"` satırı.
  3. `connections.json` içindeki `password_env` adlarının `mcp.json` `env` anahtarlarıyla **harfi harfine aynı** olduğunu kontrol et. (Eşleşmeyen profil "broken" görünür; diğer profiller çalışmaya devam eder.)
- `%USERPROFILE%\.mongo-mcp` dışında bir yere kurulduysa (`MONGO_MCP_HOME`), `command` alanına tam yolu yaz.

Dosyayı yazdıktan sonra geçerli JSON olduğunu kontrol et.

### Adım 5: Copilot custom agent ekle (isteğe bağlı)

Kullanıcıya "MongoDB işleri için hazır bir custom agent ister misin?" diye sor. Evet derse `templates/mongo-dba.agent.md` dosyasını kullanıcının proje klasörüne `.github/agents/mongo-dba.agent.md` olarak kopyala (`tools: ['mongo/*']` satırı MCP sunucusunun adıyla aynı olmalıdır).

### Adım 6: Kullanıcıya son adımları söyle

1. `Ctrl+Shift+P` → **MCP: List Servers** → **mongo** → **Start Server**. Güven onayı gelirse **Trust** deyin.
2. Şifreli bağlantılarda VS Code her şifreyi bir kez soracak ve güvenli depoda saklayacak.
3. Copilot Chat → **Agent** modu → araç (🔧) simgesinde `mongo` altında 28 tool görünmeli.
4. Test için: **"mongo list_connections ve test_connection çalıştır"**.
5. Sorun olursa: **MCP: List Servers** → **mongo** → **Show Output**.

Kurulum burada biter.

---

## Sorun giderme

| Belirti | Neden / Çözüm |
|---|---|
| `install.cmd` → EXIT 10 | Python 3.10–3.14 yok veya PATH'te değil. Python 3.12'yi kurun ("Add to PATH" ve "py launcher" seçili), VS Code'u yeniden açın. |
| EXIT 11 | `%USERPROFILE%` yazılamıyor ya da antivirüs engelliyor. `set MONGO_MCP_HOME=D:\Araclar\mongo-mcp` ile başka klasöre kurun. |
| EXIT 12 | `wheelhouse` eksik veya Python sürümü desteklenmiyor (3.15+). Zip'i yeniden açın ya da Python 3.12 kullanın. |
| EXIT 14 | Zip ile Python'un mimarisi farklı. Mesajda adı geçen diğer zip'i indirin. |
| EXIT 15 / EXIT 16 / `No module named 'pywintypes'` | Yarım veya farklı mimaride eski kurulum. `%USERPROFILE%\.mongo-mcp` klasörünü silip `install.cmd`'yi tekrar çalıştırın. |
| `Authentication failed` (kod 18) | Kullanıcı adı/şifre yanlış ya da `auth_source` yanlış (kullanıcı hangi veritabanında oluşturulduysa o; çoğunlukla `admin`). |
| `Could not reach the MongoDB server` | Adres/port yanlış, firewall, replica set adı yanlış ya da TLS gerekli. Replica set'te tek üyeye bağlanmak için `"direct_connection": true`. |
| `environment variable 'MONGO_PW_...' is not set` | `connections.json` içindeki `password_env` adı `mcp.json` `env` bölümünde yok veya farklı yazılmış. İki dosyadaki adı aynı yapın, sunucuyu **Restart** edin. |
| `USERDNSDOMAIN` hatası (`windows` tipi) | Bilgisayar domain'de değil. `user` alanına Kerberos principal'ınızı yazın: `kullanici@SIRKET.LOCAL`. |
| `_OperationCancelled` / `Lost the connection` | Sürücünün sunucuyla bağlantısı kesildi (firewall/VPN uzun süreli bağlantıyı kesiyor ya da replica set üyelerinden biri erişilemez). Hata yanıtındaki `servers` listesinde her üyenin durumuna ve son hatasına bakın; erişilemeyen üye için ağ ekibinden erişim isteyin. Sürüm 1.0.1 ile varsayılan izleme yöntemi `poll`'dur; eski kurulumu güncelleyin. |
| `WinError 10061` / `Replica set is configured with internal hostnames` | Replica set üyeleri kendini bilgisayarınızdan erişilemeyen adreslerle bildiriyor. 1.0.2 ile bağlantı adresindeki sunucular sırayla doğrudan denenir (`direct_fallback`); eski kurulumu güncelleyin. Tüm sunucular reddediyorsa port ve sunucu adlarını DBA ile teyit edin. |
| `not authorized` (kod 13) | Kullanıcının bu işlem/veritabanı için yetkisi yok. |
| `Transaction numbers are only allowed on a replica set` | `run_transaction` tek sunuculu (standalone) MongoDB'de çalışmaz; replica set gerekir. |
| Sunucu başlıyor ama tool'lar görünmüyor | Chat modu **Agent** olmalı. Copilot Business/Enterprise'da "MCP servers in Copilot" politikası açık olmalı. |
| `spawn ... ENOENT` | `command` yolu yanlış; `%USERPROFILE%\.mongo-mcp\Scripts\mongo-mcp.exe` var mı kontrol edin. |

## Güncelleme ve kaldırma

- **Güncelleme:** Yeni zip'i açın ve `install.cmd`'yi tekrar çalıştırın; `mcp.json` değişmez.
- **Kaldırma:** `%USERPROFILE%\.mongo-mcp` klasörünü silin ve `mcp.json` içinden `mongo` girdisini kaldırın.
