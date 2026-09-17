# 样例文件（联调 / 前端验证）

契约全文：[docs/archive-expand.md](../docs/archive-expand.md) · 运维验收：[DELI.md §5.2.1](../DELI.md)

| 文件 | 说明 | 密码 |
|------|------|------|
| `sample.zip` | 5 × 合成 JSON（`item_*.json`）+ `__MACOSX` 噪音 → **展开 5 个子文件** | 无 |
| `sample.tar` | `a.txt` / `notes.md` / `data.json` | — |
| `sample.tar.gz` | 同上 | — |
| `sample-password.zip` | 同上三文件，**ZipCrypto 加密** | **`secret123`** |
| `outbound.xls` 等 | 其它单文件样例 | — |

## 期望行为

| 上传 | 结果 |
|------|------|
| 无密码 zip / tar / tar.gz，可识别成员 ≤8 | `archive.status=expanded`；demo 显示 **多个 pending 附件**（与多选相同） |
| 可识别成员 >8 | `too_many`；仅父包 chip；提示拆包 |
| 加密 zip + 正确 `archive_password` | 同 expanded |
| 加密 zip 无密码 / 错密码 | HTTP **400**；demo toast 失败文案 |
| 仅 `__MACOSX` / 不支持类型 | `empty`；仅父包 |

Demo：选 `.zip` 时 `prompt` 要密码（可留空）；**硬刷新**页面以免缓存旧 JS。

## curl 自检

```bash
# tar.gz
curl -s -X POST http://127.0.0.1:8020/v1/files \
  -H "Authorization: Bearer dev-api-key" -H "X-Tenant-Id: dev" \
  -F "file=@sample/sample.tar.gz" | jq '.archive.status, (.archive.children|length)'

# 加密 zip
curl -s -X POST http://127.0.0.1:8020/v1/files \
  -H "Authorization: Bearer dev-api-key" -H "X-Tenant-Id: dev" \
  -F "file=@sample/sample-password.zip" \
  -F "archive_password=secret123" | jq '.archive'
```

## 重新生成加密 zip

```bash
cd sample
mkdir -p _pack
echo 'hello from a.txt in archive' > _pack/a.txt
echo '# hello markdown' > _pack/notes.md
echo '{"k":1}' > _pack/data.json
(cd _pack && zip -P 'secret123' ../sample-password.zip a.txt notes.md data.json)
tar -cf sample.tar -C _pack a.txt notes.md data.json
tar -czf sample.tar.gz -C _pack a.txt notes.md data.json
rm -rf _pack
```

## 自动化测试

```bash
python3 -m pytest tests/test_archive_expand.py -v
```
