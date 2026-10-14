# moi-ia-bot

Bot de DMs de **Moi.IA** (@moi_ali.ia): cuando alguien comenta la palabra que pide un post
("Comenta TOOLS…" / "Comment TOOLS…"), le manda por DM el link de la guía y le contesta en
público "te lo mandé por DM". Funciona en Instagram y en la página de Facebook.

Corre en GitHub Actions cada 5 min. El repo es **público** para que los minutos de Actions sean
gratis e ilimitados; los tokens van en *Secrets*, que no se ven aunque el repo sea público.
Los logs de Actions son públicos: el bot solo imprime IDs de comentario y la palabra, nunca nombres.

## Archivos
- `scripts/auto_dm.py`: el bot (solo librería estándar de Python).
- `docs/guias.csv`: palabra → guía → link. Si cambias una guía en el repo principal, cópiala aquí.
- `.github/workflows/auto-dm.yml`: el horario.

## Cómo decide
- Lee los posts de los últimos 60 días y los comentarios de los últimos 7 (Meta solo permite la
  respuesta privada dentro de 7 días).
- La palabra sale del pie del post. En posts en inglés manda la guía `<PALABRA>_EN` si existe.
- No guarda estado: si la cuenta ya respondió el comentario (el bot, ManyChat o tú), lo salta.
- Máximo 25 comentarios por corrida; se detiene si Meta bloquea la acción o falta un permiso.

## Encenderlo
1. **Secrets** (Settings → Secrets and variables → Actions → *New repository secret*), los mismos
   del repo Moi_Ali.IA: `IG_USER_ID`, `IG_ACCESS_TOKEN`, `FB_PAGE_ID`, `FB_PAGE_ACCESS_TOKEN`,
   `META_GRAPH_VERSION`.
2. **Diagnóstico:** Actions → *Auto-DM Moi.IA* → *Run workflow* → modo `diagnostico`. Marca ✅/❌
   cada permiso del token.
3. **Prueba:** modo `dry_run` (muestra qué haría) y luego `enviar`.
4. **Prender el horario:** variable `AUTO_DM_ACTIVO` = `true` (pestaña *Variables*). Opcional
   `AUTO_DM_REDES` = `ig` para solo Instagram. Antes apaga ManyChat para no contestar dos veces.

## Qué pide Meta
| Para | Permiso | Nivel |
|---|---|---|
| Leer comentarios y responder en público (IG) | `instagram_basic`, `instagram_manage_comments` | Standard (tu propia cuenta) |
| Leer/responder comentarios (FB) | `pages_read_engagement`, `pages_read_user_content`, `pages_manage_engagement` | Standard |
| **Mandar el DM** | `instagram_manage_messages` (IG), `pages_messaging` (FB) | **Advanced Access**: App Review + verificación del negocio |

Con Standard Access, Meta solo deja mandar DMs a cuentas con rol en la app (testers). Pasos:
regenerar el token del usuario del sistema con esos permisos, probar con una cuenta tester y
grabar la prueba (es el video que pide App Review), verificar el negocio (como persona física:
RFC/constancia de situación fiscal y comprobante de domicilio) y pedir Advanced Access.

## Ojo
- GitHub pausa los horarios de un repo público tras 60 días sin commits; si te llega el correo,
  entra a Actions y vuelve a habilitar el workflow.
- Respuesta privada: una por comentario. Si la persona contesta el DM, hay 24 h para seguir.
