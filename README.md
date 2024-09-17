# Slowstore
Easy to use, object store that stores your objects as JSON files(per object). 

It directly maps your python objects to JSON files and every object is auto synced to its coresponding JSON file (by default). 

It is designed to be easy to plug into your program, without need of servers, connection strings, or any setup just set the directory where you would like to store your files.

**Slowstore*  it is intended to be used for exploration and debugging of my program behaviour.

#### Installation
```bash
pip install slowstore
```

## Usage
You can create a new Slowstore instance by calling the constructor:

```python
from slowstore import Slowstore
from pydantic import BaseModel

class SampleModel(BaseModel):
    name: str
    age: int = 0
    def birthday(self):
        self.age += 1

store = Slowstore[SampleModel](SampleModel, "mydata", key_selector=lambda x: x.name)  # `key_selector` function is used to compose the key by which the object is tracked in the store)
dennis = store.add(name="denis", age=32)
dennis.name = "DENIS"  # here the name in the json will also change from "dennis" to "DENIS"
dennis.birthday()      # will also trigger another change in the age field and it will be reflected in the json file. 
```
`mydata/dennis.json` after running this program will look like:
```json
{
  "name": "DENIS",
  "age": 33,
  "__key__": "denis",
  "__changes__": [
    {
      "__key__": "denis",
      "prop_name": "age",
      "prev_val": 32,
      "new_val": 33,
      "date": "2024-08-28T19:04:12.840353"
    },
    {
      "__key__": "denis",
      "prop_name": "name",
      "prev_val": "denis",
      "new_val": "DENIS",
      "date": "2024-08-28T19:04:12.840216"
    },
  ]
}
```
`Slowstore` tracks what happened in your small program on every field.
You can toggle the `save_on_change` flat at any time after the store is created, 
or you can also set the `save_on_change` flag to your liking in the constructor of the `store`

```python
store.save_on_change = False 
```

After this code is executed, you will need to run `store.commit(some_model)` or store.commit_all() in order to persist the changes. 

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

### Check if some object or it's key is in the store

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


## How it works

The slowtore instance behaves like a dictionary of objects you added to it. Instead of storing the object it creates a proxy object that stores the object's state and the changes you made to it. 

When you commit the changes, the proxy object is updated with the new state, and the changes are saved to disk.

When you undo/redo the changes, the proxy object is updated with the previous state, and the changes are saved to disk.

Check the ModelProxy class for more details.

## Important

Slow store is **slow** and it is not intended to be used in multithreaded contexts. It's primarily created to provide a good DX while working on a certain feature.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.



