import boto3
from botocore.exceptions import ClientError
import sys

def create_organization_table():
    print("Creating table: kinetic-organizations...")
    dynamodb = boto3.client('dynamodb', region_name='ap-south-1')
    
    table_name = 'kinetic-organizations'
    
    try:
        # Create Table
        response = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {
                    'AttributeName': 'orgId',
                    'KeyType': 'HASH'  # Partition key
                }
            ],
            AttributeDefinitions=[
                {
                    'AttributeName': 'orgId',
                    'AttributeType': 'S'  # String
                }
            ],
            BillingMode='PAY_PER_REQUEST'
        )
        print(f"Table creation initiated. Status: {response['TableDescription']['TableStatus']}")
        
        # Wait until the table exists
        print("Waiting for table to become ACTIVE...")
        waiter = dynamodb.get_waiter('table_exists')
        waiter.wait(TableName=table_name)
        print(f"Success! Table '{table_name}' is now ACTIVE and ready for use.")
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceInUseException':
            print(f"Table '{table_name}' already exists.")
        else:
            print(f"Failed to create table: {e.response['Error']['Message']}")
            sys.exit(1)

if __name__ == '__main__':
    create_organization_table()
