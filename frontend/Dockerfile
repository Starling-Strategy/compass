# ===========================================
# NCTQ Compass Frontend - Dockerfile
# Multi-stage build for PHP + Tailwind CSS
# ===========================================

# -----------------
# Stage 1: Build CSS
# -----------------
FROM node:20-alpine AS css-builder

WORKDIR /app

# Copy package files
COPY package*.json ./
COPY tailwind.config.js ./
COPY src/ ./src/
COPY public/ ./public/

# Install dependencies and build CSS
RUN npm ci && npm run build-css

# -----------------
# Stage 2: PHP Runtime
# -----------------
FROM php:8.3-apache

# Install system dependencies and PHP extensions
RUN apt-get update && apt-get install -y \
    libcurl4-openssl-dev \
    libzip-dev \
    unzip \
    git \
    && docker-php-ext-install curl \
    && rm -rf /var/lib/apt/lists/*

# Install Composer
COPY --from=composer:latest /usr/bin/composer /usr/bin/composer

# Configure Apache
ENV APACHE_DOCUMENT_ROOT=/var/www/html/public
RUN sed -ri -e 's!/var/www/html!${APACHE_DOCUMENT_ROOT}!g' /etc/apache2/sites-available/*.conf
RUN sed -ri -e 's!/var/www/!${APACHE_DOCUMENT_ROOT}!g' /etc/apache2/apache2.conf /etc/apache2/conf-available/*.conf

# Enable Apache modules for SSE streaming
RUN a2enmod rewrite headers proxy proxy_http

# Configure Apache for SSE (Server-Sent Events)
RUN echo '<Directory /var/www/html/public>\n\
    Options -Indexes +FollowSymLinks\n\
    AllowOverride All\n\
    Require all granted\n\
    # Disable output buffering for SSE\n\
    SetEnv no-gzip 1\n\
    # Security headers\n\
    Header always set X-Content-Type-Options "nosniff"\n\
    Header always set Content-Security-Policy "frame-ancestors '"'"'self'"'"' https://nctq.org https://*.nctq.org https://nctq.ai https://*.nctq.ai"\n\
    Header always set Referrer-Policy "strict-origin-when-cross-origin"\n\
    # Force revalidation of JS/CSS so deploys reach existing users without per-import ?v= bookkeeping\n\
    <FilesMatch "\.(js|css)$">\n\
        Header set Cache-Control "no-cache, must-revalidate"\n\
    </FilesMatch>\n\
</Directory>' > /etc/apache2/conf-available/sse.conf \
    && a2enconf sse

# Set working directory
WORKDIR /var/www/html

# Copy composer files first (for better caching)
COPY composer.json ./

# Install PHP dependencies
RUN composer install --no-dev --optimize-autoloader --no-interaction

# Copy application files
COPY . .

# Copy built CSS from Stage 1
COPY --from=css-builder /app/public/assets/css/index.css ./public/assets/css/index.css

# Set correct permissions
RUN chown -R www-data:www-data /var/www/html \
    && chmod -R 755 /var/www/html

# Expose port 80
EXPOSE 80

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost/ || exit 1

# Start Apache
CMD ["apache2-foreground"]
