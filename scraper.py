import asyncio
import re
import logging
import asyncpg
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from playwright.async_api import async_playwright

# ==========================================
# CONFIGURATION
# ==========================================
# Update these credentials to match your local PostgreSQL setup
DB_CONFIG = {
    "user": "postgres",
    "password": "your_password",
    "database": "leads_db",
    "host": "localhost",
    "port": "5432"
}

# Configure logging for visibility
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ==========================================
# REGEX PATTERNS
# ==========================================
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
EXCLUDED_EMAILS_REGEX = re.compile(r"^(sentry|admin|noreply|no-reply|test|do-not-reply|mailer-daemon)\@", re.IGNORECASE)
IMAGE_EXTENSIONS_REGEX = re.compile(r"\.(png|jpg|jpeg|gif|svg|webp|ico)(\?.*)?$", re.IGNORECASE)

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def is_valid_email(email: str) -> bool:
    """Validates an email address and filters out generic or invalid ones."""
    if not email or '@' not in email: return False
    if len(email) > 254 or len(email) < 5: return False
    if EXCLUDED_EMAILS_REGEX.match(email): return False
    if IMAGE_EXTENSIONS_REGEX.search(email): return False
    return True

def get_instagram_url(href: str) -> str | None:
    """Extracts and cleans Instagram profile URLs, filtering out posts/reels."""
    match = re.search(r"https?://(www\.)?instagram\.com/([A-Za-z0-9_.]+)", href)
    if match:
        handle = match.group(2)
        # Ignore non-profile paths
        if handle.lower() not in {'p', 'reel', 'stories', 'explore', 'accounts', 'direct', 'about', 'legal'}:
            return f"https://instagram.com/{handle}/"
    return None

def extract_data_from_html(html: str, base_url: str):
    """Parses HTML to extract brand name, emails, and Instagram URLs."""
    soup = BeautifulSoup(html, 'html.parser')
    emails = set()
    instagram_url = None
    
    # Extract Brand Name
    brand_name = None
    if soup.title and soup.title.string:
        brand_name = soup.title.string.strip()
    elif soup.find('meta', property='og:site_name'):
        brand_name = soup.find('meta', property='og:site_name').get('content', '').strip()
        
    # Extract Emails and Socials from links
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        
        # Mailto extraction
        if href.startswith('mailto:'):
            email = href.replace('mailto:', '').split('?')[0].strip()
            if is_valid_email(email):
                emails.add(email)
        
        # Instagram extraction
        if 'instagram.com' in href and not instagram_url:
            ig_link = get_instagram_url(href)
            if ig_link:
                instagram_url = ig_link
                
    # Regex extraction from raw page text
    text = soup.get_text(separator=" ", strip=True)
    for match in EMAIL_REGEX.finditer(text):
        email = match.group(0)
        if is_valid_email(email):
            emails.add(email)
            
    return emails, instagram_url, brand_name

def find_contact_link(soup: BeautifulSoup, base_url: str) -> str | None:
    """Finds the most likely contact/about page link from the homepage."""
    keywords = ['contact', 'about', 'support', 'about us', 'contact us', 'contacto']
    for a_tag in soup.find_all('a', href=True):
        text = a_tag.get_text(strip=True).lower()
        href = a_tag['href']
        if any(keyword in text for keyword in keywords):
            return urljoin(base_url, href)
    return None

# ==========================================
# DATABASE & BROWSER LOGIC
# ==========================================
async def init_db():
    """Initializes the database connection and ensures the table exists."""
    conn = await asyncpg.connect(**DB_CONFIG)
    create_table_query = """
        CREATE TABLE IF NOT EXISTS leads (
            id SERIAL PRIMARY KEY,
            website_url VARCHAR UNIQUE NOT NULL,
            brand_name VARCHAR,
            email_addresses TEXT[],
            instagram_url VARCHAR,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """
    await conn.execute(create_table_query)
    logging.info("Database initialized.")
    return conn

async def save_to_db(conn, website_url: str, emails: list, instagram_url: str | None, brand_name: str | None):
    """Upserts the scraped data into PostgreSQL."""
    if not emails:
        logging.info(f"No valid emails found for {website_url}. Skipping database insertion.")
        return

    query = """
        INSERT INTO leads (website_url, brand_name, email_addresses, instagram_url, scraped_at)
        VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
        ON CONFLICT (website_url) 
        DO UPDATE SET 
            email_addresses = EXCLUDED.email_addresses,
            instagram_url = EXCLUDED.instagram_url,
            brand_name = EXCLUDED.brand_name,
            scraped_at = CURRENT_TIMESTAMP;
    """
    # asyncpg automatically converts Python lists to Postgres TEXT[] arrays
    await conn.execute(query, website_url, brand_name, emails, instagram_url)
    logging.info(f"Successfully saved/updated data for {website_url}")

async def process_url(context, url: str, conn):
    """Handles the entire scraping lifecycle for a single URL."""
    page = None
    try:
        page = await context.new_page()
        logging.info(f"Navigating to homepage: {url}")
        
        # Navigate and wait for network idle, strict 15s timeout
        await page.goto(url, wait_until="networkidle", timeout=15000)
        html = await page.content()
        
        # Initial extraction on homepage
        emails, instagram_url, brand_name = extract_data_from_html(html, url)
        
        # Deep Crawl: Look for Contact/About pages
        soup = BeautifulSoup(html, 'html.parser')
        contact_url = find_contact_link(soup, url)
        
        if contact_url:
            try:
                logging.info(f"Found contact link. Deep crawling: {contact_url}")
                await page.goto(contact_url, wait_until="networkidle", timeout=15000)
                contact_html = await page.content()
                
                # Extract from contact page
                contact_emails, _, _ = extract_data_from_html(contact_html, contact_url)
                emails.update(contact_emails)
            except Exception as e:
                logging.warning(f"Failed to load or parse contact page {contact_url}: {e}")
                
        # Save to DB
        await save_to_db(conn, url, list(emails), instagram_url, brand_name)
        
    except Exception as e:
        logging.error(f"Error processing {url}: {type(e).__name__} - {e}")
    finally:
        if page:
            await page.close()

async def main(seed_urls: list):
    """Main execution function."""
    conn = await init_db()
    
    async with async_playwright() as p:
        # Launch headless Chromium
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        )
        
        # Process URLs sequentially to prevent IP bans
        for url in seed_urls:
            await process_url(context, url, conn)
            
        await browser.close()
    await conn.close()
    logging.info("Scraping process fully completed.")

# ==========================================
# EXECUTION
# ==========================================
if __name__ == "__main__":
    SEED_URLS = [
        "https://example-supplement.com",
        "https://example-tech.com"
    ]
    
    # Run the async event loop
    asyncio.run(main(SEED_URLS))