CREATE CONSTRAINT customer_id IF NOT EXISTS FOR (n:Customer) REQUIRE n.customerId IS UNIQUE;
CREATE CONSTRAINT account_id  IF NOT EXISTS FOR (n:Account)  REQUIRE n.accountId  IS UNIQUE;
CREATE CONSTRAINT merchant_id IF NOT EXISTS FOR (n:Merchant) REQUIRE n.merchantId IS UNIQUE;
CREATE CONSTRAINT snapshot_id IF NOT EXISTS FOR (n:Snapshot) REQUIRE n.snapshotId IS UNIQUE;
CREATE INDEX account_status IF NOT EXISTS FOR (n:Account) ON (n.status);
