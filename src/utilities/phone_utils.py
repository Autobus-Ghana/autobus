import re
import logging

logger = logging.getLogger(__name__)

def extract_ghana_phone_numbers_from_text(text: str) -> list:
    """
    Extract all valid Ghana phone numbers from text (e.g., OCR output).
    
    Handles:
    - Local format: 0XXXXXXXXX (10 digits starting with 0)
    - International format: 233XXXXXXXXX (12 digits)
    - Text with OCR noise, headers, etc.
    
    Args:
        text: Text potentially containing Ghana phone numbers
        
    Returns:
        List of valid Ghana phone numbers in local format (0XXXXXXXXX)
    """
    if not text:
        return []
    
    # Pattern 1: Match 10-digit sequences starting with 0
    # This catches local format: 0XXXXXXXXX
    pattern1 = r'\b0\d{9}\b'
    
    # Pattern 2: Match 12-digit sequences starting with 233
    # This catches international format: 233XXXXXXXXX
    pattern2 = r'\b233\d{9}\b'
    
    # Pattern 3: Match 9-digit sequences that could be phone numbers
    # (without leading 0, could be partial format)
    pattern3 = r'(?<!\d)\d{9}(?!\d)'
    
    phones = []
    
    # Find all matches from pattern 1 (most common locally)
    for match in re.finditer(pattern1, text):
        phone = match.group()
        if phone not in phones:
            phones.append(phone)
            logger.info(f"[PHONE_EXTRACT] Found phone in local format: {phone}")
    
    # Find all matches from pattern 2 (international format)
    for match in re.finditer(pattern2, text):
        phone = match.group()
        # Convert to local format
        local_phone = convert_to_local_ghana_format(phone)
        if local_phone not in phones:
            phones.append(local_phone)
            logger.info(f"[PHONE_EXTRACT] Found phone in international format: {phone} -> {local_phone}")
    
    # Find all matches from pattern 3, but validate with Ghana network prefixes
    ghana_prefixes = ['024', '025', '053', '054', '055', '059',  # MTN
                      '020', '050',  # Vodafone
                      '023', '026', '027', '056', '057', '058']  # AirtelTigo
    
    for match in re.finditer(pattern3, text):
        partial_phone = match.group()
        # Try as 9-digit without prefix
        candidate_0prefix = '0' + partial_phone
        # Check if it has a valid Ghana prefix
        if candidate_0prefix[:3] in ghana_prefixes and candidate_0prefix not in phones:
            phones.append(candidate_0prefix)
            logger.info(f"[PHONE_EXTRACT] Found phone with Ghana prefix: {candidate_0prefix}")
    
    return phones


def clean_ocr_text(text: str) -> str:
    """
    Clean OCR extracted text by removing common noise patterns.
    
    Removes:
    - Debug output patterns like "lebe_backend  |"
    - Multiple consecutive spaces
    - Lines that are only special characters
    
    Args:
        text: Raw OCR text
        
    Returns:
        Cleaned text
    """
    if not text:
        return text
    
    # Remove common debug/logging patterns
    text = re.sub(r'lebe_backend\s*\|', '', text)
    text = re.sub(r'\[.*?\]\s*', '', text)  # Remove [tags]
    
    # Remove lines with only pipes/special chars
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        # Skip lines that are mostly special characters or just whitespace
        if line.strip() and not re.match(r'^[\|\s\-\_\*]+$', line):
            cleaned_lines.append(line)
    
    text = '\n'.join(cleaned_lines)
    
    # Remove multiple consecutive newlines
    text = re.sub(r'\n\n+', '\n', text)
    
    # Remove multiple consecutive spaces
    text = re.sub(r'  +', ' ', text)
    
    logger.debug(f"[OCR_CLEAN] Cleaned OCR text")
    return text


def normalize_ghana_phone_number(phone: str) -> str:
    """
    Normalize Ghanaian phone numbers to international format (233XXXXXXXXX).

    Rules:
    - If 10 digits starting with 0: Remove 0, add 233 prefix
      Example: 0550748724 -> 233550748724
    - If already starts with 233: Keep as is
      Example: 233550748724 -> 233550748724
    - If has + prefix: Remove it
      Example: +233550748724 -> 233550748724

    Args:
        phone: Phone number string (may have spaces, dashes, etc.)

    Returns:
        Normalized phone number in format 233XXXXXXXXX
    """
    if not phone:
        return phone

    # Remove all non-digit characters (spaces, dashes, parentheses, etc.)
    cleaned_phone = re.sub(r'\D', '', phone)

    # If empty after cleaning, return original
    if not cleaned_phone:
        logger.warning(f"Phone number has no digits: {phone}")
        return phone

    # Case 1: 10-digit number starting with 0 (local format)
    # Example: 0550748724 -> 233550748724
    if len(cleaned_phone) == 10 and cleaned_phone.startswith('0'):
        normalized = '233' + cleaned_phone[1:]
        logger.info(f"Normalized phone: {phone} -> {normalized}")
        return normalized

    # Case 2: Already in international format with 233
    # Example: 233550748724 -> 233550748724
    elif cleaned_phone.startswith('233') and len(cleaned_phone) == 12:
        logger.info(f"Phone already normalized: {phone} -> {cleaned_phone}")
        return cleaned_phone

    # Case 3: 9-digit number without leading 0 (partial local format)
    # Example: 550748724 -> 233550748724
    elif len(cleaned_phone) == 9:
        normalized = '233' + cleaned_phone
        logger.info(f"Normalized phone: {phone} -> {normalized}")
        return normalized

    # Case 4: Invalid format - log warning and return cleaned version
    else:
        logger.warning(f"Unexpected phone format: {phone} (cleaned: {cleaned_phone})")
        return cleaned_phone


def convert_to_local_ghana_format(phone: str) -> str:
    """
    Convert Ghanaian phone numbers to local format (0XXXXXXXXX).

    Rules:
    - If starts with 233: Remove 233, add 0 prefix
      Example: 233550748724 -> 0550748724
    - If already starts with 0: Keep as is
      Example: 0550748724 -> 0550748724
    - If has + prefix: Remove it and process
      Example: +233550748724 -> 0550748724

    Args:
        phone: Phone number string (in 233 or 0 format)

    Returns:
        Phone number in local format 0XXXXXXXXX
    """
    if not phone:
        return phone

    # Remove all non-digit characters (spaces, dashes, parentheses, etc.)
    cleaned_phone = re.sub(r'\D', '', phone)

    # If empty after cleaning, return original
    if not cleaned_phone:
        logger.warning(f"Phone number has no digits: {phone}")
        return phone

    # International dialing prefix: 00233550748724 -> 233550748724
    if cleaned_phone.startswith('00') and len(cleaned_phone) > 2:
        cleaned_phone = cleaned_phone[2:]

    # WhatsApp sometimes sends the trunk 0 after the country code:
    # 2330550748724 (13 digits) -> 0550748724
    if cleaned_phone.startswith('2330') and len(cleaned_phone) == 13:
        cleaned_phone = cleaned_phone[3:]

    # Case 1: International format with 233 prefix (12 digits total)
    # Example: 233550748724 -> 0550748724
    if cleaned_phone.startswith('233') and len(cleaned_phone) == 12:
        local_format = '0' + cleaned_phone[3:]
        logger.info(f"Converted phone to local format: {phone} -> {local_format}")
        return local_format

    # Case 2: Already in local format with 0
    # Example: 0550748724 -> 0550748724
    elif cleaned_phone.startswith('0') and len(cleaned_phone) == 10:
        logger.info(f"Phone already in local format: {phone} -> {cleaned_phone}")
        return cleaned_phone

    # Case 3: 9-digit number without leading 0 (assume international without 233)
    # Example: 550748724 -> 0550748724
    elif len(cleaned_phone) == 9 and not cleaned_phone.startswith('0'):
        local_format = '0' + cleaned_phone
        logger.info(f"Converted phone to local format: {phone} -> {local_format}")
        return local_format

    # Case 4: Invalid format - log warning and return as is
    else:
        logger.warning(f"Unexpected phone format for local conversion: {phone} (cleaned: {cleaned_phone})")
        return cleaned_phone
