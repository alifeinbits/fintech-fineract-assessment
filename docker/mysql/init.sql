CREATE DATABASE IF NOT EXISTS fineract_tenants CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS fineract_default CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS mock_erp CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS integration CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'erp'@'%' IDENTIFIED BY 'erp';
CREATE USER IF NOT EXISTS 'integration'@'%' IDENTIFIED BY 'integration';
GRANT ALL PRIVILEGES ON mock_erp.* TO 'erp'@'%';
GRANT ALL PRIVILEGES ON integration.* TO 'integration'@'%';
GRANT ALL PRIVILEGES ON fineract_tenants.* TO 'root'@'%';
GRANT ALL PRIVILEGES ON fineract_default.* TO 'root'@'%';
FLUSH PRIVILEGES;
