import os

def main():
    filePath,extList = getUserInput()
    print(extList)
    fileList,fileRoots = getFileList(filePath,extList)
    strippedFileList,modifiedFileList = processFile(fileList,fileRoots)
    # print(strippedFileList)
    renameFile(modifiedFileList,strippedFileList)



def getUserInput():
    filePath = input("Enter full file path for the downloaded files from patreon downloader: ")
    fileExtensionsToProcess = input("Enter file extensions of the files to process, separated by semicolon: ")
    if len(fileExtensionsToProcess) > 0:
        extList = fileExtensionsToProcess.split(";")
        for index, ext in enumerate(extList):
            ext = "." + ext
            extList[index] = ext
    else:
        extList = []

    return filePath,extList

def getFileList(filePath:str,extList:list):
    fileList = []
    fileRoots = []
    for root, dirs, files in os.walk(filePath):
        for file in files:
            # print(f"{os.path.splitext(os.path.join(root, file))[1]}")
            if len(extList):
                if os.path.splitext(os.path.join(root, file))[1] in extList:
                    fileList.append(os.path.join(root, file))
                    fileRoots.append(root)
                else:
                    print(f"{os.path.join(root, file)} is not a file to be processed, skipping...")
            else:
                fileList.append(os.path.join(root, file))
                fileRoots.append(root)
    return fileList,fileRoots

def processFile(fileList:list,fileRoots:list):
    strippedFileList = []
    modifiedFileList = []
    for index,file in enumerate(fileList):
        fileNameSplit = file.split("_")
        if len(fileNameSplit) > 2 and fileNameSplit[1].isdigit():
            filename = fileNameSplit[2:]
            strippedFileName = "_".join(filename)
            # print(strippedFileName)
            if len(strippedFileName) > 0:
                strippedFileList.append(os.path.join(fileRoots[index],strippedFileName))
                modifiedFileList.append(fileList[index])
            else:
                continue
        else:
            print(f"{file} is does not have the prefix, skipping...")
    return strippedFileList,modifiedFileList

def renameFile(fileList,strippedFileList):
    for index,newFileName in enumerate(strippedFileList):
        try:
            os.rename(fileList[index], newFileName)
        except FileNotFoundError as e:
            print(f"{fileList[index]} can not be renamed, skipping...")
            continue


if __name__ == "__main__":
    main()