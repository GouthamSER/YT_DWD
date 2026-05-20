"""
Admin Handlers for Cookies Management
"""
import os
import shutil
import logging
from datetime import datetime
from contextlib import suppress

from pyrogram import filters
from config import is_admin, save_cookies_to_db, log_admin_action, get_latest_cookies_info, get_admin_logs

logger = logging.getLogger("YTBot")

# ═══════════════════════════════════════════
#    ADMIN COOKIES.TXT HANDLERS
# ═══════════════════════════════════════════

async def setup_admin_handlers(app):
    """Setup all admin handlers"""
    
    @app.on_message(filters.command("setcookies") & filters.private)
    async def set_cookies(client, message):
        """Admin command: Upload new cookies.txt file"""
        user_id = message.from_user.id
        
        if not is_admin(user_id):
            await message.reply_text("❌ You don't have permission to update cookies.")
            return
        
        if not message.document:
            await message.reply_text(
                "📝 Usage:\nReply with a cookies.txt file to update.\n\n"
                "Example: `/setcookies` (while replying to a file)"
            )
            return
        
        # Check if it's a text file
        if message.document.mime_type not in ["text/plain", "application/octet-stream"]:
            await message.reply_text(
                f"❌ Invalid file type: {message.document.mime_type}\n"
                "Please send a `.txt` file"
            )
            return
        
        msg = await message.reply_text("⏳ Processing cookies.txt...")
        
        try:
            # Download the file
            file_path = await client.download_media(
                message.document,
                file_name="cookies_temp.txt"
            )
            
            # Validate it's a valid cookies file (basic check)
            with open(file_path, 'r') as f:
                content = f.read()
                if len(content) < 50:  # Too small to be valid
                    await msg.edit_text("❌ File seems too small to be valid cookies.txt")
                    with suppress(Exception): os.remove(file_path)
                    return
            
            # Backup old cookies
            cookies_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
            if os.path.exists(cookies_path):
                backup_path = cookies_path + f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                shutil.copy2(cookies_path, backup_path)
                logger.info(f"Backed up cookies to: {backup_path}")
            
            # Replace with new cookies
            shutil.move(file_path, cookies_path)
            
            # Save to database
            await save_cookies_to_db(cookies_path, user_id, "Uploaded via /setcookies command")
            
            # Log admin action
            await log_admin_action(user_id, "SET_COOKIES", f"File size: {os.path.getsize(cookies_path)} bytes")
            
            await msg.edit_text(
                f"✅ **Cookies Updated!**\n\n"
                f"👤 Admin: `{user_id}`\n"
                f"📦 Size: `{os.path.getsize(cookies_path)}` bytes\n"
                f"⏰ Time: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
            )
            
        except Exception as e:
            logger.error(f"Error updating cookies: {e}")
            await msg.edit_text(f"❌ Error: {str(e)[:100]}")
    
    @app.on_message(filters.command("refreshcookies") & filters.private)
    async def refresh_cookies(client, message):
        """Admin command: Refresh cookies.txt status"""
        user_id = message.from_user.id
        
        if not is_admin(user_id):
            await message.reply_text("❌ You don't have permission to refresh cookies.")
            return
        
        try:
            cookies_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
            
            if not os.path.exists(cookies_path):
                await message.reply_text(
                    "❌ cookies.txt not found!\n"
                    "Use `/setcookies` to upload one."
                )
                return
            
            file_size = os.path.getsize(cookies_path)
            file_mtime = datetime.fromtimestamp(os.path.getmtime(cookies_path))
            
            # Log the refresh action
            await log_admin_action(user_id, "REFRESH_COOKIES", f"File verified: {file_size} bytes")
            
            await message.reply_text(
                f"✅ **Cookies Status OK**\n\n"
                f"📁 File: `cookies.txt`\n"
                f"📦 Size: `{file_size}` bytes\n"
                f"⏰ Last Modified: `{file_mtime.strftime('%Y-%m-%d %H:%M:%S')}`\n"
                f"✨ Status: **Active**"
            )
            
        except Exception as e:
            logger.error(f"Error refreshing cookies: {e}")
            await message.reply_text(f"❌ Error: {str(e)[:100]}")
    
    @app.on_message(filters.command("cookiesinfo") & filters.private)
    async def cookies_info(client, message):
        """Admin command: View cookies info from database"""
        user_id = message.from_user.id
        
        if not is_admin(user_id):
            await message.reply_text("❌ You don't have permission to view this.")
            return
        
        try:
            info = await get_latest_cookies_info()
            
            if not info:
                await message.reply_text(
                    "ℹ️ No cookies history in database.\n"
                    "Upload your first cookies using `/setcookies`"
                )
                return
            
            admin_mention = f"`{info['admin_id']}`"
            timestamp = info['timestamp'].strftime('%Y-%m-%d %H:%M:%S') if 'timestamp' in info else "N/A"
            
            text = (
                f"📋 **Latest Cookies Info**\n\n"
                f"👤 Uploaded by: {admin_mention}\n"
                f"📦 Size: `{info.get('file_size', 0)}` bytes\n"
                f"⏰ Time: `{timestamp}`\n"
                f"📝 Notes: `{info.get('notes', 'N/A')}`\n"
                f"✨ Status: `{info.get('status', 'unknown')}`"
            )
            
            await message.reply_text(text)
            
        except Exception as e:
            logger.error(f"Error fetching cookies info: {e}")
            await message.reply_text(f"❌ Error: {str(e)[:100]}")
    
    @app.on_message(filters.command("admins") & filters.private)
    async def show_admins(client, message):
        """Admin command: Show list of admins"""
        user_id = message.from_user.id
        
        if not is_admin(user_id):
            await message.reply_text("❌ You don't have permission to view this.")
            return
        
        from config import ADMIN_IDS
        
        if not ADMIN_IDS:
            await message.reply_text(
                "ℹ️ **No admins configured**\n"
                "Everyone can use admin commands.\n\n"
                "To restrict: Set `ADMIN_IDS` environment variable\n"
                "Example: `ADMIN_IDS=123456789,987654321`"
            )
        else:
            admin_list = "\n".join(f"• `{admin_id}`" for admin_id in ADMIN_IDS)
            await message.reply_text(
                f"👥 **Admin List** (`{len(ADMIN_IDS)}`)\n\n{admin_list}"
            )
    
    @app.on_message(filters.command("adminlogs") & filters.private)
    async def admin_logs(client, message):
        """Admin command: Show recent admin actions"""
        user_id = message.from_user.id
        
        if not is_admin(user_id):
            await message.reply_text("❌ You don't have permission to view this.")
            return
        
        try:
            logs = await get_admin_logs(limit=10)
            
            if not logs:
                await message.reply_text("📭 No admin logs found.")
                return
            
            text = "📋 **Recent Admin Actions** (Last 10)\n\n"
            for log in logs:
                timestamp = log['timestamp'].strftime('%m-%d %H:%M') if 'timestamp' in log else "N/A"
                admin_id = log.get('admin_id', 'N/A')
                action = log.get('action', 'UNKNOWN')
                details = log.get('details', '')
                
                detail_str = f" - `{details[:40]}`" if details else ""
                text += f"⏰ `{timestamp}` | 👤 `{admin_id}` | 🔧 `{action}`{detail_str}\n"
            
            await message.reply_text(text)
            
        except Exception as e:
            logger.error(f"Error fetching admin logs: {e}")
            await message.reply_text(f"❌ Error: {str(e)[:100]}")
    
    @app.on_message(filters.command("exportcookies") & filters.private)
    async def export_cookies(client, message):
        """Admin command: Export current cookies.txt file"""
        user_id = message.from_user.id
        
        if not is_admin(user_id):
            await message.reply_text("❌ You don't have permission to export cookies.")
            return
        
        try:
            cookies_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
            
            if not os.path.exists(cookies_path):
                await message.reply_text(
                    "❌ cookies.txt not found!"
                )
                return
            
            await message.reply_document(
                cookies_path,
                caption="📄 Current cookies.txt file"
            )
            
            # Log the action
            await log_admin_action(user_id, "EXPORT_COOKIES", "Exported cookies.txt")
            
        except Exception as e:
            logger.error(f"Error exporting cookies: {e}")
            await message.reply_text(f"❌ Error: {str(e)[:100]}")
    
    logger.info("✅ Admin handlers registered")
