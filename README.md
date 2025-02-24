# wallet reporter

This is a non-official reporter for **wallet** application written in python. Site: https://web.budgetbakers.com/ .

Unfortunately, the site doesn't provide any data exporting options and
the [Rest API was removed long time ago](https://support.budgetbakers.com/hc/en-us/articles/10761479741586-Rest-API). 

## Installation

```shell
git clone git@github.com:zerlok/wallet-reporter.git
cd wallet-reporter
poetry install
```

## pywallet

Python code for integration with wallet (e.g. browser client).

## Usage

## setup

Create `.env` file with appropriate credentials.

```
BUDGETBACKERS_EMAIL=login@email.com
BUDGETBACKERS_PASSWORD=login-password-on-budgetbackers-site
```

### Incomes & Expenses CSV report

Create incomes & expenses report by categories & months for last 3 months.

```shell
pywallet \
  --filter "report for budget plan" \
  ie \
  incomes-expenses-report.csv \
  -n 3
```

Example of `incomes-expenses-report.csv` data:

```csv
category,2024.11,2024.12,2025.01
Income,1.33,2.09,3.19
Food & Beverages,-1.12,-2.09,-3.95
Shopping,-1.84,-2.99,-3.03
Housing,-1.21,-2.04,-3.23
Transportation,-1.52,-2.06,-3.85
Vehicle,0.00,0.00,0.00
Life & Entertainment,-1.26,-2.61,-3.55
"Communication, PC",-1.74,-2.93,-3.23
Financial expenses,-1.26,-2.48,-3.17
Investments,-1.38,-2.21,-3.86
Others,-1.15,-2.87,3.24
Unknown,0.00,0.00,0.00
```
