# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

ans.An index is like looking up fast for what we needed directly without reading/searching the entire database.
1) uniq_emp_code (employees):I have used Employees index so that we can fetch employee details accurately/fast without searching every record.  
2) dept_emp_code:Quickly lists one department’s employees, already in code order.   
3) uniq_emp_date:The database refuses a second record for the same person on the same day.  
4) date_emp (attendance):Records stored in date order, newest first, then by code.  
5) status_date_emp (attendance):The same ordering, but grouped by status first.  
6) emp_punch_in (attendance):One person’s records in punch-in order.    
7) joined_dept (employees)	:Employees ordered by joining date, then department.						

I thought about date index.I left it out because date_emp already starts with date, so a separate one would be a duplicate that slows every write.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

ans: If two identical punch my code validates as:
1) If both requests arrive together. FastAPI handles them at the same time, each in its own worker.   
2) Each one does the same checks. The body is validated (a bad one gets 422). Then the employee is looked up (an unknown one gets 404).
3) Each one works out the same values. The punch time is cut down to whole seconds, the attendance date is worked out in IST, and the late minutes are calculated. Since the requests are identical, both end up with the same record.
4) Each one tries to save its record with insert_one. Your code does not look for an existing record first.
5) The database makes the decision. It has a rule, the unique index on employee code and date, that allows only one record per employee per day. It lets one insert in. That request gets 201 and the saved record back. The other insert breaks the rule and the database raises a duplicate-key error.
6) Your code catches that error and turns it into a 409 with the message “already punched in for this date”. Nothing is saved for that request.

The end result is one record in the database, one 201 and one 409. It doesn’t matter which request wins.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?  
ans: 
1)Every one gets the rank first as per their late minutes.Anyone with the same late minutes will share the same rank.  
2)Then the limit is applied to the rank for for the number of rows.  
3)Even the limit is 1 and we have 2 employees with rank 1 will be returned in the leaderboard.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?

ans:1)It starts from the list of employees, not from the attendance records. This is the main idea. If it started from the records, someone with no logs would have nothing to appear in, and they’d be missed.  
2)It keeps everyone who had joined by the end of the month. The rule is joined_on on or before the last day of that month. A person who joins the next month is left out, and a person who joined mid-month counts for the whole month.  
3)For each employee, it looks up that month’s records. Some people will have several records, some will have none.  
4)It keeps people with no records, with zeros. An employee with nothing to attach still gets one row, with all their totals at 0. This is what the “preserve empty” setting on the unwind step does. Without it, anyone with no records would silently disappear.  
5)It counts the rows per department. Every employee is exactly one row, so the headcount is just the number of rows in each department, whether or not they have any logs.

5. **One thing you would change** if this had to serve 100x the data.    

ans: change-Running totals per employee per month.   
problem-The monthly report, the department summary and the leaderboard all re-read a whole month of daily records on every request. With 100x the data, that means 100x the reading.	  
change-Keep a small summary record per employee per month (present days, late minutes and so on) and update it whenever someone punches in, punches out or is corrected. Reports then read a few small records.		