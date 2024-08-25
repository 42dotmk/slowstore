# Slowstore

This is a very simple single file single class key-value store written in Python. It is designed to be slow and inefficient, and is intended to be used for testing purposes only.
The rules are simple:
Every store has it's own directory, and every item is stored in a separate JSON file.

That's it.

It is easy to use and inspect the data being manipulated.

## Installation

You can install Slowstore using pip:

```bash
pip install slowstore
```

## Usage
You can create a new Slowstore instance by calling the constructor:

```python
from slowstore import Slowstore

class SampleModel(BaseModel):
    name: str
    age: int = 0

# Create a new store and load the data 
store = Slowstore[SampleModel](SampleModel, "mydata")).load()
```

**One feature that I use a lot is to commit on property change, so I can inspect exactly what is going on**

```python
store.save_on_change = True
```
you can also set this from the constructor

```python
store = Slowstore[SampleModel](SampleModel, "mydata", save_on_change=True).load()
```
After this code is executed, every time a property is changed, 
the changes will be saved to disk immediately.

This is slow, but it is useful for debugging and testing.


### Adding items to the store

```python
s1 = store.upsert(SampleModel(name="Alice", age=30))
s2 = store.upsert(SampleModel(name="Bob", age=25))
s3 = store.upsert(SampleModel(name="Charlie", age=35))
store.commit_all()

s1.age = 32
```

### Commit the changes to the store

```python
store.commit(s1)
store.commit(s2)
# or just commit all changes
store.commit_all() 
```

### Undo/Redo
every slowstore item is a proxy object around what you've created. This allows you to access the object's attributes and adds undo/redo/dirty functionality

```python
s1.undo()
s1.redo()
```

### Querying
```python
# get all items, returns an iterator
all_items = store.all()

# yield all items that match a condition
filtered_items = store.filter(lambda x: x.age > 30)

# get the first item that matches a condition
first_item = store.first(lambda x: x.age > 30)

# Check if some object or it's key is in the store
if s2 in store:
    print("s2 is in store")
else:
    print("s2 is not in store")

```

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.


