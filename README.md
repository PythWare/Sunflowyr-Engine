# Sunflowyr Engine

Sunflowyr Engine is high end modding software for RWBY Grimm Eclipse. Scroll to the bottom of the readme if you want to see GUI examples of the software

It's a modding engine I developed to make modding RWBY GE significantly easier and pleasant to use. It's RWBY inspired.

Sunflowyr Engine will also include other software I made such as Loose Builder (used for creating loose file mods), AssetBundle Generator (used for generating new AssetBundle files for RWBY GE to load, will be explained further down in AssetBundle Generator section), Asset Finder, Asset Inspector, etc for usage as well.

# Details

SE unpacks everything from the RWBY GE containers, assigns taildata to an external json, converts various formats to easier to edit formats (GLB, PNG, wav, etc) but it still keeps the original Unity formats incase modders want to work with the Unity format versions, etc.

New assets can be used in RWBY GE with Sunflowyr Engine without having to replace original assets

I suggest reading SE_Guide.txt within the Guide folder before using SE

# What's needed to use SE

Python, Pillow, numpy. Pretty small requirements tho make sure you have the latest Pillow (11.2)/numpy(1.24) installed

To install pillow and numpy, open up your command prompt and type `python -m pip install Pillow numpy`

Once you have those 3 requirements, all you have to do to use SE is double click on main.pyw. It should popup with the scroll GUI. If for some reason it doesnt (faulty python install, association issues, etc) then open up cmd in the directory you have SE installed at and type `python main.pyw`

I may someday translate the Python code to C or Rust so that python isn't needed but it's a lot of fucking code for making RWBY GE accessible for modding, as of 11/22/2026 the codebase is near 50k lines of code. So if it bothers you to download python, get the fuck over it or get out!

Now if you're on linux or mac, idk how well SE will work for you. I develop on windows and I can't test my software on an OS I don't own. I can vouch for SE working on windows. So for linux, I suggest using wine with SE if you intend to use the Shader Compiler. For mac users, why are you using mac to play a windows built game? Get a real computer for playing games!

# Unpack speed

SE's unpacking speed scales with your hardware, it's fuckin fast but for most people it will probably take 5 or so minutes for a full unpack. That's just how it is, get the fuck over it! It's a lot of work being done by SE to do a full unpack (parsing, decompressing, conversions to editable formats, etc). For the fastest possible unpacking speed, make sure you're unpacking to a SSD instead of HDD and set your windows defender/antivirus to exclude the folder you unpack RWBY GE too cause otherwise that will impact your unpack speed.

# Loose Builder, used for Loose Mods

Loose Builder is used for installing the loader if it already isn't installed and compiling modded unpacked files/new assets to be used by RWBY GE into formats Unity expects. What that means is modders mod whatever the fuck they want, when finished they open Loose Builder, enter the metadata in Mod Info tab, and then press the Build Mod button. Very easy

To share mods, zip your mods with the zip button in the Installed Mods tab.

# Asset Finder

Asset Finder may be the most useful part of SE besides the unpacking and Loose Builder. Asset Finder makes modders' job far easier, it's more useful than windows explorer for locating files for RWBY GE imo. You can easily type in the search bar for explorer the name of a file and see the directory but you can't be automatically told what bundle that file came from ya know? But Asset Finder can, whereas explorer would choke on some of the modding tasks, Asset Finder can do a lot of things explorer can't. You can search for assets just like explorer, search based on the unity path ID, list all files belonging to a bundle, etc.

# Bundle Inspector

Think of it as a tool for the curious, you'd use the main unpacker at the home screen instead of BI. What Bundle Inspector is useful for is inspecting inside assetbundles. You can use BI on RWBY GE's vanilla bundles if desired but really, BI is most useful for inspecting a modder's AssetBundle. Like, suppose you want to find out what files were modded for older mods made years ago (back then modders made AssetBundle mods). You can use BI to inspect inside those bundles modders made back then, if you have permission from them you could unpack their bundles and recreate their mod as an updated loose mod or updated AssetBundle.

So like I said, BI is a tool for the curious.

# Shader Compiler

Useful if you intend to make Shader mods. It can compile your HLSL code to DXBC

# AssetBundle Generator

Sunflowyr Engine's AssetBundle Generator can create valid AssetBundles for RWBY GE to use without needing Unity. What that means is you can make mods by building an AssetBundle without needing Unity installed.

By default I have AssetBundle Generator greyed out until it's clicked on because I didn't get enough testers for testing AssetBundle mods made with SE before release. So the option exists to use AssetBundle Generator to create AssetBundle mods (just click the greyed out icon) but I highly suggest most modders use Loose Builder instead for loose mods.

# Sunflowyr Engine updates

Updates for Sunflowyr Engine will be displayed in the Sunflowyr Engine updates panel, so instead of reading a readme for update notes you can read within the software.

# GUI examples

<img width="1919" height="1032" alt="se1" src="https://github.com/user-attachments/assets/8e85e31f-4500-49c3-8927-6d14c16eaa81" />

<img width="1912" height="1040" alt="se2" src="https://github.com/user-attachments/assets/967f5d2d-f71a-4488-b5e4-9d65e11f30c8" />

<img width="1920" height="1036" alt="se3" src="https://github.com/user-attachments/assets/a22fc841-5491-4b2d-b278-cbd8a011133e" />

<img width="1920" height="1034" alt="se4" src="https://github.com/user-attachments/assets/830d8b74-cfc8-478e-a59b-59c78c637fbd" />

<img width="1910" height="1026" alt="se5" src="https://github.com/user-attachments/assets/0c6d8c12-a571-4a3c-b94a-d343e06b240e" />

<img width="1914" height="1044" alt="12" src="https://github.com/user-attachments/assets/012538e5-0cdc-4473-b2de-2b6022654d42" />

<img width="1912" height="1031" alt="14" src="https://github.com/user-attachments/assets/b5014b42-f80e-49df-9991-6222f83812d7" />

<img width="1915" height="1031" alt="13" src="https://github.com/user-attachments/assets/c93f0f7f-a598-4c79-b45f-de9c6434749c" />

<img width="1918" height="1034" alt="15" src="https://github.com/user-attachments/assets/dbc81529-59ca-4d62-9d77-07f5a5442e4d" />

<img width="1915" height="1034" alt="se8" src="https://github.com/user-attachments/assets/06c58580-7ddc-4f19-a177-7c1fad0c9404" />

<img width="1919" height="1046" alt="se9" src="https://github.com/user-attachments/assets/4e3028f4-3e72-4283-8644-302bfa2cd059" />

<img width="1915" height="1037" alt="se5" src="https://github.com/user-attachments/assets/364d2e26-bef0-44e4-8539-f5b37f2b657c" />

# Mod examples

Yang's Shotgun particles changed to Lilac, Yang's ingame textures changed, and even playable Team BRIM and Team CFVY (yes, you can expand the playable roster and create new teams for RWBY GE) mod examples

<img width="618" height="581" alt="pa1" src="https://github.com/user-attachments/assets/98f4d815-4385-4f7d-be54-35c3360399d0" />

<img width="1280" height="732" alt="yang_6" src="https://github.com/user-attachments/assets/6ace6e99-3e88-4b74-b688-bb6e426a5ed2" />

<img width="1280" height="720" alt="cfvy" src="https://github.com/user-attachments/assets/209ecc7b-acd1-4c47-a3e8-156b1e0b9b78" />

<img width="1280" height="720" alt="brim" src="https://github.com/user-attachments/assets/cffd2229-da3c-45bf-bdf1-92b43d7ccb75" />

<img width="1280" height="720" alt="bakugo" src="https://github.com/user-attachments/assets/185c54bd-169d-4569-8933-befac79a2b24" />

<img width="1280" height="720" alt="mirko" src="https://github.com/user-attachments/assets/e7ecb784-0fd4-42fa-990d-b053e34efc69" />

<img width="1280" height="720" alt="izuku" src="https://github.com/user-attachments/assets/c8dbc830-6619-4f13-8eea-6cdafc4088f0" />

<img width="1280" height="720" alt="allmight" src="https://github.com/user-attachments/assets/36112f32-6791-4e0b-a45d-519676f479c7" />

<img width="827" height="562" alt="might" src="https://github.com/user-attachments/assets/59db1e17-16df-4bda-9f84-b5dc2ac939e3" />
