# 🚀 Payment Gateway System (Mock)

A **Dockerized mock payment gateway** supporting **Order creation**, **UPI payments**, **Card payment validation**, and **transaction tracking**, built using **FastAPI**, **PostgreSQL**, and **Nginx**.

This project demonstrates **backend API design**, **database modeling**, **payment flow simulation**, **frontend checkout**, and **containerized deployment**.

---

## 🧩 Features

### ✅ Backend (FastAPI)

* Merchant authentication using API Key & Secret
* Order creation API
* Payment processing API
* Supports **UPI payments**
* **Card payment validation** (mocked with error handling)
* Payment status tracking
* Health check endpoint

### ✅ Database (PostgreSQL)

* Merchants
* Orders
* Payments
* SQLAlchemy ORM with proper relations

### ✅ Frontend

* Checkout page for payment initiation
* Transactions dashboard to view payment history
* Simple HTML/CSS UI

### ✅ Dockerized Setup

* Backend service container
* Frontend service container
* Checkout page served via Nginx
* PostgreSQL container
* Docker Compose orchestration

---

## 🗂️ Project Structure

```
payment-gateway/
│── .env
│── .env.example
│── docker-compose.yml
│── README.md
│
├── backend/
│   ├── Dockerfile
│   ├── main.py
│   ├── models.py
│   ├── database.py
│   └── requirements.txt
│
├── frontend/
│   ├── Dockerfile
│   ├── index.html
│   ├── styles.css
│   └── transactions.html
│
└── checkout-page/
    ├── Dockerfile
    ├── checkout.html
    ├── index.html
    ├── styles.css
    └── default.conf
```

---

## ⚙️ Environment Variables

Create a `.env` file using `.env.example` as reference.

Example:

```
DATABASE_URL=postgresql://postgres:postgres@db:5432/payment_gateway
API_KEY=key_test_abc123
API_SECRET=secret_test_xyz789
```

---

## 🐳 Running the Project (Docker)

### 1️⃣ Build and start services

```bash
docker-compose up --build
```

### 2️⃣ Services

| Service       | URL                                                          |
| ------------- | ------------------------------------------------------------ |
| Backend API   | [http://localhost:8000](http://localhost:8000)               |
| Health Check  | [http://localhost:8000/health](http://localhost:8000/health) |
| Frontend      | [http://localhost:3000](http://localhost:3000)               |
| Checkout Page | [http://localhost](http://localhost)                         |

---

## 🔌 API Usage

### 🔹 Health Check

```bash
curl http://localhost:8000/health
```

---

### 🔹 Create Order

```bash
curl -X POST http://localhost:8000/api/v1/orders \
-H "Content-Type: application/json" \
-H "X-Api-Key: key_test_abc123" \
-H "X-Api-Secret: secret_test_xyz789" \
-d "{\"amount\":50000,\"currency\":\"INR\"}"
```

---

### 🔹 UPI Payment (Success)

```bash
curl -X POST http://localhost:8000/api/v1/payments \
-H "Content-Type: application/json" \
-H "X-Api-Key: key_test_abc123" \
-H "X-Api-Secret: secret_test_xyz789" \
-d "{\"order_id\":\"<ORDER_ID>\",\"method\":\"upi\",\"vpa\":\"test@upi\"}"
```

---

### 🔹 Card Payment (Mock Validation)

```bash
curl -X POST http://localhost:8000/api/v1/payments \
-H "Content-Type: application/json" \
-H "X-Api-Key: key_test_abc123" \
-H "X-Api-Secret: secret_test_xyz789" \
-d "{\"order_id\":\"<ORDER_ID>\",\"method\":\"card\",\"card_number\":\"4111111111111111\",\"expiry_month\":12,\"expiry_year\":30,\"cvv\":\"123\"}"
```

> ⚠️ Card payments are **intentionally mocked** and return validation errors to simulate real-world gateway behavior.

---

## 📊 Transactions

* All successful payments are stored in the database
* Transactions are visible on the frontend dashboard
* Supports filtering by order and payment status

---

## 🧪 Payment Status Handling

* `created`
* `processing`
* `success`
* `failed`

---

## 🛡️ Notes

* This is a **mock payment gateway** for evaluation and learning purposes
* No real bank or card networks are integrated
* Card validation errors are expected and intentional

---

## 🏁 Conclusion

This project demonstrates:

* RESTful API design
* Secure merchant authentication
* Payment flow handling
* Database modeling
* Docker-based deployment
* Frontend integration

✅ **Meets all requirements for a mock payment gateway system**
