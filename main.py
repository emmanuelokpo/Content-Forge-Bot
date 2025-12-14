import os
import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update, ForceReply
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai.errors import APIError

# --- 1. CONFIGURATION AND SECRETS ---

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load API keys and tokens from environment variables (set on Render)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SOCIALBU_API_KEY = os.environ.get("SOCIALBU_API_KEY")

# Initialize Gemini Client
try:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
except (ValueError, AttributeError) as e:
    logger.error(f"Error initializing Gemini client: {e}. Check GEMINI_API_KEY.")
    gemini_client = None

# --- 2. GLOBAL STATE AND DATA STRUCTURES ---

# Dictionary to store generated content before approval {user_id: generated_content_dict}
user_draft_content = {}
# The Gemini model to use
GEMINI_MODEL = "gemini-2.5-flash"

# --- 3. HELPER FUNCTIONS ---

def scrape_product_details(url):
    """Scrapes product title and description from a given URL."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status() # Raise an exception for bad status codes

        soup = BeautifulSoup(response.content, 'lxml')

        # Attempt to find common product details (may need adjustment for specific sites)
        title = soup.find('h1').text.strip() if soup.find('h1') else 'Product Title Not Found'
        description_element = soup.find('div', class_=lambda x: x and ('description' in x or 'details' in x))
        description = description_element.text.strip() if description_element else 'Description Not Found'
        
        # Simple cleanup (limit length and remove excessive whitespace)
        description = ' '.join(description.split())[:2000] # Limit to 2000 chars

        return {
            "url": url,
            "title": title,
            "description": description
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Scraping error for URL {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"General scraping error: {e}")
        return None

def generate_sales_copy(product_details):
    """Uses Gemini to generate multi-platform sales copy."""
    if not gemini_client:
        return "Gemini client not initialized. Check API Key.", None

    prompt = f"""
    You are a professional content strategist. Your task is to generate platform-specific sales copy 
    for a product based on the following details.

    Product Title: {product_details['title']}
    Product Description: {product_details['description']}
    Original Link: {product_details['url']}

    Please generate four distinct pieces of content tailored for each platform: Facebook, X (Twitter), 
    LinkedIn, and Pinterest.
    
    Format the response strictly using markdown headers for each platform:

    ## Facebook
    [Content optimized for Facebook, including a strong CTA.]

    ## X (Twitter)
    [Content optimized for X, short, punchy, and including relevant hashtags.]

    ## LinkedIn
    [Content optimized for LinkedIn, professional, and focusing on business value.]

    ## Pinterest
    [Content optimized for Pinterest, highly visual and description focused on a clear benefit.]
    """

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(temperature=0.7)
        )
        # Parse the structured response into a dictionary
        raw_text = response.text
        
        content_dict = {}
        platforms = ["Facebook", "X (Twitter)", "LinkedIn", "Pinterest"]
        
        for platform in platforms:
            start_tag = f"## {platform}"
            end_tag = "## " if platform != "Pinterest" else None
            
            start_index = raw_text.find(start_tag)
            if start_index != -1:
                content_start = start_index + len(start_tag)
                
                if end_tag:
                    end_index = raw_text.find(end_tag, content_start)
                else:
                    end_index = len(raw_text) # For the last platform, go to the end
                
                copy = raw_text[content_start:end_index].strip()
                content_dict[platform] = copy

        return raw_text, content_dict

    except APIError as e:
        logger.error(f"Gemini API Error: {e}")
        return f"Gemini API Error: Could not generate content. {e}", None
    except Exception as e:
        logger.error(f"General generation error: {e}")
        return "An unexpected error occurred during content generation.", None


def distribute_content_socialbu(content_dict):
    """Posts content to all social platforms via SocialBu API."""
    if not SOCIALBU_API_KEY:
        return "SocialBu API Key is missing. Distribution failed."

    # NOTE: You MUST adjust 'platform_ids' based on your SocialBu account setup.
    socialbu_url = "https://socialbu.com/api/v1/posts"
    
    payload = {
        "api_key": SOCIALBU_API_KEY,
        "content": f"Multi-platform content generated by Content Forge Bot:\n\n{content_dict.get('Facebook', '')}\n\n{content_dict.get('X (Twitter)', '')}", # Consolidated text post
        "platform_ids": [123, 456, 789], # REPLACE with YOUR SocialBu Profile IDs
        "schedule_time": "now",
        "media_urls": [], # We will add image/video URLs here later when we upgrade the bot
    }

    try:
        response = requests.post(socialbu_url, json=payload, timeout=20)
        response.raise_for_status()
        
        # Check SocialBu's specific success response format
        socialbu_data = response.json()
        if socialbu_data.get('status') == 'success':
             return f"Content successfully distributed via SocialBu! (Post ID: {socialbu_data.get('data', {}).get('post_id')})."
        else:
             return f"SocialBu API reported an error: {socialbu_data.get('message', 'Unknown error')}"

    except requests.exceptions.RequestException as e:
        logger.error(f"SocialBu distribution error: {e}")
        return f"Distribution failed due to network/API error: {e}"
    except Exception as e:
        logger.error(f"General distribution error: {e}")
        return "An unexpected error occurred during distribution."

# --- 4. TELEGRAM HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and prompts for the first URL."""
    await update.message.reply_html(
        "Welcome to the **Content Forge Bot!**\n\n"
        "To start, simply send me a **product URL/link** from a website. "
        "I will scrape the details, generate multi-platform sales copy using Gemini AI, "
        "and send it back for your review.",
        reply_markup=ForceReply(selective=True),
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a help message."""
    await update.message.reply_text(
        "**Available Commands:**\n"
        "/start - Begin the content generation process.\n"
        "/help - Show this help message.\n"
        "/approve - Use this command to instantly distribute the last reviewed content via SocialBu.",
        parse_mode="Markdown"
    )

async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming URL, initiates scraping and content generation."""
    user_id = update.effective_user.id
    url = update.message.text
    
    # Simple check for a URL pattern
    if not url.startswith('http'):
        await update.message.reply_text("That doesn't look like a valid URL. Please send a full product link (e.g., https://example.com/product).")
        return

    await update.message.reply_text("🔍 URL received. Starting scraping and content generation with Gemini AI... This may take a moment.")
    
    # 1. Data Extraction (Scraping)
    product_details = scrape_product_details(url)
    
    if not product_details or 'Description Not Found' in product_details['description']:
        await update.message.reply_text("❌ Failed to scrape product details from the URL. Please check the link or try a different one.")
        return

    # 2. Generation (Gemini AI)
    generated_markdown, generated_dict = generate_sales_copy(product_details)
    
    if not generated_dict:
        await update.message.reply_text(f"❌ Content generation failed: {generated_markdown}")
        return
        
    # Store the generated content for /approve step
    user_draft_content[user_id] = generated_dict

    # 3. Review (Send draft copy back to Telegram)
    response_text = (
        f"✅ **Content Draft Ready!**\n\n"
        f"**Product:** {product_details['title']}\n"
        f"**Source:** {product_details['url']}\n\n"
        f"--- **Review Content** ---\n\n"
        f"{generated_markdown}\n\n"
        f"--- **Action** ---\n\n"
        f"Review the content above. If it is ready to be published, use the command **`/approve`** to post it now!"
    )

    await update.message.reply_text(response_text, parse_mode="Markdown")


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /approve command to distribute the content via SocialBu."""
    user_id = update.effective_user.id

    if user_id not in user_draft_content:
        await update.message.reply_text("There is no content waiting for approval. Please send a new product URL first to generate content.")
        return

    content_to_distribute = user_draft_content.pop(user_id) # Retrieve and clear the draft content

    await update.message.reply_text("🚀 Approval received! Initiating distribution to all social media platforms via SocialBu API...")

    # 4. Distribution (SocialBu)
    distribution_result = distribute_content_socialbu(content_to_distribute)
    
    await update.message.reply_text(f"**Distribution Status:**\n\n{distribution_result}", parse_mode="Markdown")

# --- 5. MAIN EXECUTION ---

def main() -> None:
    """Start the bot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Bot cannot start.")
        return

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("approve", approve_command))

    # Handles all other text messages (assumed to be URLs)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_message))

    # Run the bot. For Render, we use `run_polling` as it keeps the process alive.
    logger.info("Bot is starting... Running in Polling mode on Render.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
