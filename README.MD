# pylint
```
pyenv activate cv311
pylint <file> or <dir>
```

# cloudwatch log insights query 

## 1. To find if there are videos not uploaded
### 1-1. Query
```
fields @timestamp, @message
| filter @message like /HTTPSConnectionPool/
| display @timestamp, @logStream, @message
| sort @timestamp asc
| limit 200
```

### 1-2. Query
```
fields @timestamp, @message
| filter @message like /put_object, Exception/
| display @timestamp, @logStream, @message
| sort @timestamp asc
| limit 200
```

## 2. To find if there are "file too close issue"
### 2-1. Query
```
fields @timestamp, @message
| filter @message like /splitmuxsink-fragment-closed is too close to splitmuxsink-fragment-opened/
| display @timestamp, @logStream, @message
| sort @timestamp desc
| limit 200
```

## 3. To find if there are rtsp seek issues
### 3-1. Query
```
fields @timestamp, @message
| filter @message like /Downstream is not seekable/
| display @timestamp, @logStream, @message
| sort @timestamp desc
| limit 200
```