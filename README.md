# Slowstore

This is simple,single file, key-value store that stores your objects as JSON on file system. 

It is designed to be easy to plug into your program, no servers, no connection strings, nothing, just set the directory where you would like to store your files.

**Slowstore is slow**, it is intended to be used for exploration.

## Installation

You can install Slowstore using pip:

```bash
pip install slowstore
```

## How is the data organized?

The data is stored in a directory, with each item stored in a separate file. The file name is the key of the item, and the content is the JSON representation of the object.

That's all there is to it.

## Features

At the moment Slowstore works with Pydantic models, but I plan to add support for other types of objects that are serializable to JSON.

- [X] **Save on change**: Set the store to save the data every time a property is changed
- [X] **Undo/Redo**: Undo and redo changes to the object
- [X] **Dirty**: Check if the object has been changed
- [X] **Filtering**: Filter/Query the items in the store
- [X] **Deleting**: Delete items from the store
- [X] **Commit**: Commit changes to the store
- [ ] **Partial load**: Load only the items you need, and lazy load the rest
- [ ] **Transactions**: Add support for transactions
- [ ] **Non-Pydantic objects**: Add support for other types of objects
- [ ] **Indexes**: Add indexes to the store to speed up queries

## Motivation

I created Slowstore because I wanted a simple way to store my objects on disk, without having to worry about databases, connection strings, or any of that. 
I want to be able to inspect the data, see what is going on with my program undo changes if needed, and filter the data easily.

## Usage
You can create a new Slowstore instance by calling the constructor:

```python
from slowstore import Slowstore

class SampleModel(BaseModel):
    name: str
    age: int = 0

    def birthday(self):
        self.age += 1

store = Slowstore[SampleModel](SampleModel, "mydata", save_on_change=True))


# This is how you add or update an object in the store
dennis = store.upsert("dennis", SampleModel(name="denis", age=32))

# immediately after previous line is evaluated,
# you will have a json file (mydata/dennis.json) represening this object

dennis.name = "DENIS"
# here the name in the json will also change from "dennis" to "DENIS"
# also the associated change will be tracked so you can further inspect if needed.

dennis.birthday()
# will also trigger another change in the age field and it will be reflected in the json file. 

```


You can  enable/disable the save_on_change flat at any time after the store is created

```python
store.save_on_change = False
```

After this code is executed, every time a property is changed, 
the changes will be saved to disk immediately.

This is **slow**, but it is useful for debugging and testing.

### Commit the changes to the store
If the store's `save_on_change` flag is not set to `True`, you can commit the changes manually.
```python
store.commit(s1)
store.commit(s2)
# or just commit all changes
store.commit_all() 
```

### Undo/Redo

Every slowstore item is a proxy object around the object you added to the store. This allows you to access the object's attributes and adds undo/redo/dirty functionality

```python
s1.undo()
s1.redo()
```

### Querying the store

```python
# get all items, returns an iterator
all_items = store.all()

# yield all items that match a condition
filtered_items = store.filter(lambda x: x.age > 30)

# get the first item that matches a condition
first_item = store.first(lambda x: x.age > 30)
```

# Check if some object or it's key is in the store

```python
if s2 in store:
    print("s2 is in store")
else:
    print("s2 is not in store")
```

### Deleting items
Any of the following will delete the item from the store, along with the file that contains the data

```python
store.delete(s1)
del store[s2]
key = "some_key"
store.delete(key)
```

## How it works

The slowtore instance behaves like a dictionary of objects you added to it. Instead of storing the object it creates a proxy object that stores the object's state and the changes you made to it. 

When you commit the changes, the proxy object is updated with the new state, and the changes are saved to disk.

When you undo/redo the changes, the proxy object is updated with the previous state, and the changes are saved to disk.

Check the ModelProxy class for more details.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.



