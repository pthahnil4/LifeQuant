import datetime
# 本文件是测试时间相关的函数


# 当前日期和时间
now = datetime.datetime.now()
print("当前日期和时间:", now)

# 指定日期和时间
specific_date = datetime.datetime(2024, 7, 22, 13, 45, 0)
print("指定日期和时间:", specific_date)

# 今天的日期
today = datetime.date.today()
print("今天的日期:", today)

# 指定时间
specific_time = datetime.time(13, 45, 0)
print("指定时间:", specific_time)

# 日期加减
tomorrow = today + datetime.timedelta(days=1)
print("明天的日期:", tomorrow)

# 时间差
start_time = datetime.datetime(2024, 7, 22, 13, 0, 0)
end_time = datetime.datetime(2024, 7, 22, 14, 0, 0)
duration = end_time - start_time
print("时间差:", duration)

# 格式化日期和时间
formatted_date = now.strftime("%Y-%m-%d %H:%M:%S")
print("格式化日期和时间:", formatted_date)

# 从字符串解析日期和时间
date_string = "2024-07-22 13:45:00"
parsed_date = datetime.datetime.strptime(date_string, "%Y-%m-%d %H:%M:%S")
print("解析后的日期和时间:", parsed_date)
