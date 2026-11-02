"""Metadata-based groups, not content validation."""
CATEGORIES = {
    'image': ('jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tif', 'tiff', 'heic'),
    'video': ('mp4', 'mov', 'avi', 'mkv', '3gp', 'webm'),
    'audio': ('mp3', 'wav', 'aac', 'm4a', 'flac', 'ogg'),
    'document': ('pdf', 'txt', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'csv'),
}


def category(file_type):
    value = str(file_type).lower()
    return next((name for name, extensions in CATEGORIES.items() if value in extensions), 'other')
