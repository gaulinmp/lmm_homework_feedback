# Accounting Data Analytics

**Instructor:** Mac Gaulin

## Course Description
This course provides accounting students with an introduction to data analytics as used in the accounting profession. The course covers accounting data, analytical methods, and applications across financial, managerial, audit, forensic, and tax accounting. The focus is on understanding and communicating analytics insights and conclusions through hands-on application and practice.

## Course Structure
The course is designed to equip students with the gamut of data analytics skills required by the modern profession.
- **Labs**: Smaller deliverables focused on application of the week's topics.
- **Projects**: Multi-week endeavors tying together skills. The first three projects use data about firm fundamentals and market performance. The final project is a capstone project where students apply the skills learned to their own analytics questions.

## Weekly Outline

### Week 1: Introduction and Class Overview
- **Topic**: Overview of analytics and its application in Accounting.
- **[Lab 1](labs_hw/week1_opening-data)**: Open and view datasets in Excel, Tableau, and Python to understand the differences between each platform and their strengths for data analytics.

### Week 2: Data in Companies
- **Topic**: What comprises business data, its sources (transactional, sensors, consumer, economic), issues, storage, and access methods. The "5 Vs" of data.
- **[Lab 2](labs_hw/week2_connecting-to-data)**: Import and clean poorly formatted journal entry data by removing headers and page breaks, then explore the cleaned dataset.

### Week 3: Data Visualization
- **Topic**: Descriptive statistics, visualization as communication, types of visualizations, and best practices. (e.g., Anscombe's quartet).
- **[Lab 3](labs_hw/week3_visualization)**: Create four visualizations (line graph, histogram, scatter plot, and box plot) using financial statement data for all public companies to understand trends, distributions, and comparisons.

### Week 4: Exploratory Data Analysis (EDA)
- **Topic**: Initial analysis to understand data structure, data types, distributions, and missing values. Building intuition before modeling.
- **[Lab 4](labs_hw/week4_EDA)**: Explore general ledger data from a forensic accounting case, pivot revenue and COGS data, and calculate profit trends over time.
- **[Project 1](labs_hw/project1)**: Fundamental Statement Analysis using firm fundamentals and market performance data.

### Week 5: Combining Data and RDBs
- **Topic**: Relational databases, join logic, complex merges, and statistical merging.
- **[Lab 5](labs_hw/week5_RDB)**: Connect to PostgreSQL database containing daily stock returns for S&P 500 companies, aggregate to monthly frequency, and visualize return patterns and trading volumes.

### Week 6: Automation and ETL
- **Topic**: Extract-Transform-Load (ETL) processes, pipelines, storage mechanics, and automation logic (RPA).
- **Lab**: Build automated data pipelines to extract, transform, and load data for analysis.

### Week 7: Unstructured Data
- **Topic**: Types and sources of unstructured data (text, images, video, audio), feature extraction, and Natural Language Processing (NLP).
- **Lab**: Extract features from unstructured data sources and apply NLP techniques to text data.
- **[Project 2](labs_hw/project2)**: Combine fundamental statement data with market returns data for integrated analysis.

### Week 8: Analytical Modeling
- **Topic**: Analytical modeling lifecycle, types of modeling (Descriptive, Diagnostic, Predictive, Prescriptive), and fitting models.
- **[Lab 8](labs_hw/week8_analytic-overview)**: Apply the four stages of analytics (Descriptive, Diagnostic, Predictive, Prescriptive) to financial data, using scatter plots with trend lines and R² to measure relationships and inform decision-making.

### Week 9: Supervised Learning: Regression
- **Topic**: Predicting continuous values. Estimating cost functions, identifying causality.
- **[Lab 9](labs_hw/week9_regressions)**: Build and evaluate regression models for cost estimation, comparing simple volume-based models with activity-based costing approaches, and perform out-of-sample validation.

### Week 10: Supervised Learning: Classification
- **Topic**: Predicting categories (e.g., bankruptcy risk vs. status). Classifiers and their evaluation.
- **[Lab 10](labs_hw/week10_classifiers)**: Build logistic regression classifiers to predict customer payment defaults, evaluate using confusion matrices and ROC/AUC analysis, and perform cost-benefit analysis to optimize classification thresholds.
- **[Project 3](labs_hw/project3)**: Apply regression analysis to predict returns using fundamental and market data.

### Week 11: Unsupervised Learning
- **Topic**: Dimension reduction and clustering. Finding structure in data without labeled outcomes.
- **Lab**: None

### Week 12: Foundational Models, LLMs, LMMs
- **Topic**: Large Language Models (LLMs) and Large Multimodal Models (LMMs) like ChatGPT and Gemini. AI in accounting.
- **[Lab 12](labs_hw/week12_AI)**: Use GitHub Copilot to set up development environment and build a web scraper using Playwright to collect local event data and export to Excel and iCalendar formats.


## Notes
